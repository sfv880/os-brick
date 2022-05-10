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


import array
import binascii
import os

from os_brick.encryptors import base
from os_brick import exception
from oslo_concurrency import lockutils
from oslo_concurrency import processutils
from oslo_log import log as logging

synchronized = lockutils.synchronized_with_prefix('os-brick-')

LOG = logging.getLogger(__name__)


class CryptsetupEncryptor(base.VolumeEncryptor):
    """A VolumeEncryptor based on dm-crypt.

    This VolumeEncryptor uses dm-crypt to encrypt the specified volume.
    """

    def __init__(self, root_helper,
                 connection_info,
                 keymgr,
                 execute=None,
                 *args, **kwargs):
        super(CryptsetupEncryptor, self).__init__(
            root_helper=root_helper,
            connection_info=connection_info,
            keymgr=keymgr,
            execute=execute,
            *args, **kwargs)

        # Fail if no device_path was set when connecting the volume, e.g. in
        # the case of libvirt network volume drivers.
        data = connection_info['data']
        if not data.get('device_path'):
            volume_id = data.get('volume_id') or connection_info.get('serial')
            raise exception.VolumeEncryptionNotSupported(
                volume_id=volume_id,
                volume_type=connection_info['driver_volume_type'])

        # the device's path as given to libvirt -- e.g., /dev/disk/by-path/...
        self.symlink_path = connection_info['data']['device_path']

        # a unique name for the volume -- e.g., the iSCSI participant name
        self.dev_name = 'crypt-%s' % os.path.basename(self.symlink_path)

        # NOTE(lixiaoy1): This is to import fix for 1439869 from Nova.
        # NOTE(tsekiyama): In older version of nova, dev_name was the same
        # as the symlink name. Now it has 'crypt-' prefix to avoid conflict
        # with multipath device symlink. To enable rolling update, we use the
        # old name when the encrypted volume already exists.
        old_dev_name = os.path.basename(self.symlink_path)
        wwn = data.get('multipath_id')
        if self._is_crypt_device_available(old_dev_name):
            self.dev_name = old_dev_name
            LOG.debug("Using old encrypted volume name: %s", self.dev_name)
        elif wwn and wwn != old_dev_name:
            # FibreChannel device could be named '/dev/mapper/<WWN>'.
            if self._is_crypt_device_available(wwn):
                self.dev_name = wwn
                LOG.debug("Using encrypted volume name from wwn: %s",
                          self.dev_name)

        # the device's actual path on the compute host -- e.g., /dev/sd_
        self.dev_path = os.path.realpath(self.symlink_path)

    def _is_crypt_device_available(self, dev_name):
        if not os.path.exists('/dev/mapper/%s' % dev_name):
            return False

        try:
            self._execute('cryptsetup', 'status', dev_name, run_as_root=True)
        except processutils.ProcessExecutionError as e:
            # If /dev/mapper/<dev_name> is a non-crypt block device (such as a
            # normal disk or multipath device), exit_code will be 1. In the
            # case, we will omit the warning message.
            if e.exit_code != 1:
                LOG.warning('cryptsetup status %(dev_name)s exited '
                            'abnormally (status %(exit_code)s): %(err)s',
                            {"dev_name": dev_name, "exit_code": e.exit_code,
                             "err": e.stderr})
            return False
        return True

    def _get_passphrase(self, key):
        """Convert raw key to string."""
        return binascii.hexlify(key).decode('utf-8')

    def _open_volume(self, passphrase, **kwargs):
        """Open the LUKS partition on the volume using passphrase.

        :param passphrase: the passphrase used to access the volume
        """
        LOG.debug("opening encrypted volume %s", self.dev_path)

        # NOTE(joel-coffman): cryptsetup will strip trailing newlines from
        # input specified on stdin unless --key-file=- is specified.
        cmd = ["cryptsetup", "create", "--key-file=-"]

        cipher = kwargs.get("cipher", None)
        if cipher is not None:
            cmd.extend(["--cipher", cipher])

        key_size = kwargs.get("key_size", None)
        if key_size is not None:
            cmd.extend(["--key-size", key_size])

        cmd.extend([self.dev_name, self.dev_path])

        self._execute(*cmd, process_input=passphrase,
                      check_exit_code=True, run_as_root=True,
                      root_helper=self._root_helper)

    def _get_mangled_passphrase(self, key):
        """Convert the raw key into a list of unsigned int's and then a string

        """
        # NOTE(lyarwood): This replicates the methods used prior to Newton to
        # first encode the passphrase as a list of unsigned int's before
        # decoding back into a string. This method strips any leading 0's
        # of the resulting hex digit pairs, resulting in a different
        # passphrase being returned.
        encoded_key = array.array('B', key).tolist()
        return ''.join(hex(x).replace('0x', '') for x in encoded_key)

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
        except processutils.ProcessExecutionError as e:
            if e.exit_code == 2:
                # NOTE(lyarwood): Workaround bug#1633518 by attempting to use
                # a mangled passphrase to open the device..
                LOG.info("Unable to open %s with the current passphrase, "
                         "attempting to use a mangled passphrase to open "
                         "the volume.", self.dev_path)
                self._open_volume(self._get_mangled_passphrase(key), **kwargs)

        # modify the original symbolic link to refer to the decrypted device
        self._execute('ln', '--symbolic', '--force',
                      '/dev/mapper/%s' % self.dev_name, self.symlink_path,
                      root_helper=self._root_helper,
                      run_as_root=True, check_exit_code=True)

    def _get_backend_device(self):
        """Check status for the dm-crypt device and return backed device."""
        stdout, stderr = self._execute('cryptsetup', 'status', self.dev_name,
                                       run_as_root=True,
                                       check_exit_code=[0, 4],
                                       root_helper=self._root_helper)
        if not stdout:
            return None
        lines = stdout.splitlines()
        for line in lines:
            if not line:
                continue
            fields = line.split()
            if len(fields) != 2:
                continue
            name = fields[0]
            if name == 'device:':
                device = fields[1]
                if device and os.path.exists(device):
                    return device
        return None

    def _restore_device_links(self, device):
        """Restoring original links for device."""
        LOG.debug('restoring original links for device %s', device)
        self._execute('udevadm', 'trigger', device,
                      run_as_root=True,
                      check_exit_code=False,
                      root_helper=self._root_helper)

    def _close_volume(self, **kwargs):
        """Closes the device (effectively removes the dm-crypt mapping)."""
        device = self._get_backend_device()
        if not device:
            return
        LOG.debug("closing encrypted volume %s", self.dev_path)
        # NOTE(mdbooth): remove will return 4 (wrong device specified) if
        # the device doesn't exist. We assume here that the caller hasn't
        # specified the wrong device, and that it doesn't exist because it
        # isn't open. We don't fail in this case in order to make this
        # operation idempotent.
        self._execute('cryptsetup', 'remove', self.dev_name,
                      run_as_root=True, check_exit_code=[0, 4],
                      root_helper=self._root_helper)
        self._restore_device_links(device)

    @synchronized('connect_volume')
    def detach_volume(self, **kwargs):
        """Removes the dm-crypt mapping for the device."""
        self._close_volume(**kwargs)
