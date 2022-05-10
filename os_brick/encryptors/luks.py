# Copyright (c) 2013 The Johns Hopkins University/Applied Physics Laboratory
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from os_brick.encryptors import cryptsetup
from os_brick.privileged import rootwrap as priv_rootwrap
from oslo_concurrency import lockutils
from oslo_concurrency import processutils as putils
from oslo_log import log as logging

synchronized = lockutils.synchronized_with_prefix('os-brick-')

LOG = logging.getLogger(__name__)


def is_luks(root_helper, device, execute=None):
    """Checks if the specified device uses LUKS for encryption.

    :param device: the device to check
    :returns: true if the specified device uses LUKS; false otherwise
    """
    try:
        # check to see if the device uses LUKS: exit status is 0
        # if the device is a LUKS partition and non-zero if not
        if execute is None:
            execute = priv_rootwrap.execute
        execute('cryptsetup', 'isLuks', '--verbose', device,
                run_as_root=True, root_helper=root_helper,
                check_exit_code=True)
        return True
    except putils.ProcessExecutionError as e:
        LOG.warning("isLuks exited abnormally (status %(exit_code)s): "
                    "%(stderr)s",
                    {"exit_code": e.exit_code, "stderr": e.stderr})
        return False


class LuksEncryptor(cryptsetup.CryptsetupEncryptor):
    """A VolumeEncryptor based on LUKS.

    This VolumeEncryptor uses dm-crypt to encrypt the specified volume.
    """
    def __init__(self, root_helper,
                 connection_info,
                 keymgr,
                 execute=None,
                 *args, **kwargs):
        super(LuksEncryptor, self).__init__(
            root_helper=root_helper,
            connection_info=connection_info,
            keymgr=keymgr,
            execute=execute,
            *args, **kwargs)

    def _format_volume(self, passphrase, **kwargs):
        """Creates a LUKS header on the volume.

        :param passphrase: the passphrase used to access the volume
        """
        LOG.debug("formatting encrypted volume %s", self.dev_path)

        # NOTE(joel-coffman): cryptsetup will strip trailing newlines from
        # input specified on stdin unless --key-file=- is specified.
        cmd = ["cryptsetup", "--batch-mode", "luksFormat", "--key-file=-"]

        cipher = kwargs.get("cipher", None)
        if cipher is not None:
            cmd.extend(["--cipher", cipher])

        key_size = kwargs.get("key_size", None)
        if key_size is not None:
            cmd.extend(["--key-size", key_size])

        cmd.extend([self.dev_path])

        self._execute(*cmd, process_input=passphrase,
                      check_exit_code=True, run_as_root=True,
                      root_helper=self._root_helper,
                      attempts=3)

    def _open_volume(self, passphrase, **kwargs):
        """Opens the LUKS partition on the volume using passphrase.

        :param passphrase: the passphrase used to access the volume
        """
        LOG.debug("opening encrypted volume %s", self.dev_path)
        self._execute('cryptsetup', 'luksOpen', '--key-file=-',
                      self.dev_path, self.dev_name, process_input=passphrase,
                      run_as_root=True, check_exit_code=True,
                      root_helper=self._root_helper)

    def _unmangle_volume(self, key, passphrase, **kwargs):
        """Workaround for bug#1633518

        First identify if a mangled passphrase is used and if found then
        replace with the correct unmangled version of the passphrase.
        """
        mangled_passphrase = self._get_mangled_passphrase(key)
        self._open_volume(mangled_passphrase, **kwargs)
        self._close_volume(**kwargs)
        LOG.debug("%s correctly opened with a mangled passphrase, replacing "
                  "this with the original passphrase", self.dev_path)

        # NOTE(lyarwood): Now that we are sure that the mangled passphrase is
        # used attempt to add the correct passphrase before removing the
        # mangled version from the volume.

        # luksAddKey currently prompts for the following input :
        # Enter any existing passphrase:
        # Enter new passphrase for key slot:
        # Verify passphrase:
        self._execute('cryptsetup', 'luksAddKey', self.dev_path,
                      process_input=''.join([mangled_passphrase, '\n',
                                             passphrase, '\n', passphrase]),
                      run_as_root=True, check_exit_code=True,
                      root_helper=self._root_helper)

        # Verify that we can open the volume with the current passphrase
        # before removing the mangled passphrase.
        self._open_volume(passphrase, **kwargs)
        self._close_volume(**kwargs)

        # luksRemoveKey only prompts for the key to remove.
        self._execute('cryptsetup', 'luksRemoveKey', self.dev_path,
                      process_input=mangled_passphrase,
                      run_as_root=True, check_exit_code=True,
                      root_helper=self._root_helper)
        LOG.debug("%s mangled passphrase successfully replaced", self.dev_path)

    @synchronized('connect_volume')
    def attach_volume(self, context, **kwargs):
        """Shadow the device and pass an unencrypted version to the instance.

        Transparent disk encryption is achieved by mounting the volume via
        dm-crypt and passing the resulting device to the instance. The
        instance is unaware of the underlying encryption due to modifying the
        original symbolic link to refer to the device mounted by dm-crypt.
        """

        key = self._get_key(context).get_encoded()
        passphrase = self._get_passphrase(key)

        try:
            self._open_volume(passphrase, **kwargs)
        except putils.ProcessExecutionError as e:
            if e.exit_code == 1 and not is_luks(self._root_helper,
                                                self.dev_path,
                                                execute=self._execute):
                # the device has never been formatted; format it and try again
                LOG.info("%s is not a valid LUKS device;"
                         " formatting device for first use",
                         self.dev_path)
                self._format_volume(passphrase, **kwargs)
                self._open_volume(passphrase, **kwargs)
            elif e.exit_code == 2:
                # NOTE(lyarwood): Workaround bug#1633518 by replacing any
                # mangled passphrases that are found on the volume.
                # TODO(lyarwood): Remove workaround during R.
                LOG.warning("%s is not usable with the current "
                            "passphrase, attempting to use a mangled "
                            "passphrase to open the volume.",
                            self.dev_path)
                self._unmangle_volume(key, passphrase, **kwargs)
                self._open_volume(passphrase, **kwargs)
            else:
                raise

        # modify the original symbolic link to refer to the decrypted device
        self._execute('ln', '--symbolic', '--force',
                      '/dev/mapper/%s' % self.dev_name, self.symlink_path,
                      root_helper=self._root_helper,
                      run_as_root=True, check_exit_code=True)

    def _close_volume(self, **kwargs):
        """Closes the device (effectively removes the dm-crypt mapping)."""
        device = self._get_backend_device()
        if not device:
            return
        LOG.debug("closing encrypted volume %s", self.dev_path)
        # NOTE(mdbooth): luksClose will return 4 (wrong device specified) if
        # the device doesn't exist. We assume here that the caller hasn't
        # specified the wrong device, and that it doesn't exist because it
        # isn't open. We don't fail in this case in order to make this
        # operation idempotent.
        self._execute('cryptsetup', 'luksClose', self.dev_name,
                      run_as_root=True, check_exit_code=[0, 4],
                      root_helper=self._root_helper,
                      attempts=3)
        self._restore_device_links(device)
