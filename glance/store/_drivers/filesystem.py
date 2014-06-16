# Copyright 2010 OpenStack Foundation
# Copyright 2014 Red Hat, Inc.
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

"""
A simple filesystem-backed store
"""

import errno
import hashlib
import logging
import os
import urlparse

from oslo.config import cfg

import glance.store
import glance.store.driver
from glance.store import exceptions
from glance.store.common import utils
import glance.store.location
from glance.store.openstack.common.gettextutils import _
from glance.store.openstack.common import jsonutils


LOG = logging.getLogger(__name__)

_FILESYSTEM_CONFIGS = [
    cfg.StrOpt('filesystem_store_datadir',
               help=_('Directory to which the Filesystem backend '
                      'store writes images.')),
    cfg.StrOpt('filesystem_store_metadata_file',
               help=_("The path to a file which contains the "
                      "metadata to be returned with any location "
                      "associated with this store.  The file must "
                      "contain a valid JSON dict."))]


class StoreLocation(glance.store.location.StoreLocation):
    """Class describing a Filesystem URI."""

    def process_specs(self):
        self.scheme = self.specs.get('scheme', 'file')
        self.path = self.specs.get('path')

    def get_uri(self):
        return "file://%s" % self.path

    def parse_uri(self, uri):
        """
        Parse URLs. This method fixes an issue where credentials specified
        in the URL are interpreted differently in Python 2.6.1+ than prior
        versions of Python.
        """
        pieces = urlparse.urlparse(uri)
        assert pieces.scheme in ('file', 'filesystem')
        self.scheme = pieces.scheme
        path = (pieces.netloc + pieces.path).strip()
        if path == '':
            reason = _("No path specified in URI: %s") % uri
            LOG.debug(reason)
            raise exceptions.BadStoreUri('No path specified')
        self.path = path


class ChunkedFile(object):

    """
    We send this back to the Glance API server as
    something that can iterate over a large file
    """

    CHUNKSIZE = 65536

    def __init__(self, filepath):
        self.filepath = filepath
        self.fp = open(self.filepath, 'rb')

    def __iter__(self):
        """Return an iterator over the image file"""
        try:
            if self.fp:
                while True:
                    chunk = self.fp.read(ChunkedFile.CHUNKSIZE)
                    if chunk:
                        yield chunk
                    else:
                        break
        finally:
            self.close()

    def close(self):
        """Close the internal file pointer"""
        if self.fp:
            self.fp.close()
            self.fp = None


class Store(glance.store.driver.Store):

    OPTIONS = _FILESYSTEM_CONFIGS

    def get_schemes(self):
        return ('file', 'filesystem')

    def configure_add(self):
        """
        Configure the Store to use the stored configuration options
        Any store that needs special configuration should implement
        this method. If the store was not able to successfully configure
        itself, it should raise `exceptions.BadStoreConfiguration`
        """
        self.datadir = self.conf.glance_store.filesystem_store_datadir
        if self.datadir is None:
            reason = (_("Could not find %s in configuration options.") %
                      'filesystem_store_datadir')
            LOG.error(reason)
            raise exceptions.BadStoreConfiguration(store_name="filesystem",
                                                  reason=reason)

        if not os.path.exists(self.datadir):
            msg = _("Directory to write image files does not exist "
                    "(%s). Creating.") % self.datadir
            LOG.info(msg)
            try:
                os.makedirs(self.datadir)
            except (IOError, OSError):
                if os.path.exists(self.datadir):
                    # NOTE(markwash): If the path now exists, some other
                    # process must have beat us in the race condition. But it
                    # doesn't hurt, so we can safely ignore the error.
                    return
                reason = _("Unable to create datadir: %s") % self.datadir
                LOG.error(reason)
                raise exceptions.BadStoreConfiguration(store_name="filesystem",
                                                      reason=reason)

    @staticmethod
    def _resolve_location(location):
        filepath = location.store_location.path

        if not os.path.exists(filepath):
            raise exceptions.NotFound(image=filepath)

        filesize = os.path.getsize(filepath)
        return filepath, filesize

    def _get_metadata(self):
        if self.conf.glance_store.filesystem_store_metadata_file is None:
            return {}

        try:
            with open(self.conf.glance_store.filesystem_store_metadata_file, 'r') as fptr:
                metadata = jsonutils.load(fptr)
            glance.store.check_location_metadata(metadata)
            return metadata
        except exceptions.BackendException as bee:
            LOG.error(_('The JSON in the metadata file %s could not be used: '
                        '%s  An empty dictionary will be returned '
                        'to the client.')
                      % (self.conf.glance_store.filesystem_store_metadata_file, str(bee)))
            return {}
        except IOError as ioe:
            LOG.error(_('The path for the metadata file %s could not be '
                        'opened: %s  An empty dictionary will be returned '
                        'to the client.')
                      % (self.conf.glance_store.filesystem_store_metadata_file, ioe))
            return {}
        except Exception as ex:
            LOG.exception(_('An error occurred processing the storage systems '
                            'meta data file: %s.  An empty dictionary will be '
                            'returned to the client.') % str(ex))
            return {}

    def get(self, location, offset=0, chunk_size=None, context=None):
        """
        Takes a `glance.store.location.Location` object that indicates
        where to find the image file, and returns a tuple of generator
        (for reading the image file) and image_size

        :param location `glance.store.location.Location` object, supplied
                        from glance.store.location.get_location_from_uri()
        :raises `glance.store.exceptions.NotFound` if image does not exist
        """
        filepath, filesize = self._resolve_location(location)
        msg = _("Found image at %s. Returning in ChunkedFile.") % filepath
        LOG.debug(msg)
        return (ChunkedFile(filepath), filesize)

    def get_size(self, location, context=None):
        """
        Takes a `glance.store.location.Location` object that indicates
        where to find the image file and returns the image size

        :param location `glance.store.location.Location` object, supplied
                        from glance.store.location.get_location_from_uri()
        :raises `glance.store.exceptions.NotFound` if image does not exist
        :rtype int
        """
        filepath, filesize = self._resolve_location(location)
        msg = _("Found image at %s.") % filepath
        LOG.debug(msg)
        return filesize

    def delete(self, location, context=None):
        """
        Takes a `glance.store.location.Location` object that indicates
        where to find the image file to delete

        :location `glance.store.location.Location` object, supplied
                  from glance.store.location.get_location_from_uri()

        :raises NotFound if image does not exist
        :raises Forbidden if cannot delete because of permissions
        """
        loc = location.store_location
        fn = loc.path
        if os.path.exists(fn):
            try:
                LOG.debug(_("Deleting image at %(fn)s"), {'fn': fn})
                os.unlink(fn)
            except OSError:
                raise exceptions.Forbidden(_("You cannot delete file %s") % fn)
        else:
            raise exceptions.NotFound(image=fn)

    def add(self, image_id, image_file, image_size, context=None):
        """
        Stores an image file with supplied identifier to the backend
        storage system and returns a tuple containing information
        about the stored image.

        :param image_id: The opaque image identifier
        :param image_file: The image data to write, as a file-like object
        :param image_size: The size of the image data to write, in bytes

        :retval tuple of URL in backing store, bytes written, checksum
                and a dictionary with storage system specific information
        :raises `glance.store.exceptions.Duplicate` if the image already
                existed

        :note By default, the backend writes the image data to a file
              `/<DATADIR>/<ID>`, where <DATADIR> is the value of
              the filesystem_store_datadir configuration option and <ID>
              is the supplied image ID.
        """

        filepath = os.path.join(self.datadir, str(image_id))

        if os.path.exists(filepath):
            raise exceptions.Duplicate(image=filepath)

        checksum = hashlib.md5()
        bytes_written = 0
        try:
            with open(filepath, 'wb') as f:
                for buf in utils.chunkreadable(image_file,
                                               ChunkedFile.CHUNKSIZE):
                    bytes_written += len(buf)
                    checksum.update(buf)
                    f.write(buf)
        except IOError as e:
            if e.errno != errno.EACCES:
                self._delete_partial(filepath, image_id)
            errors = {errno.EFBIG: exceptions.StorageFull(),
                      errno.ENOSPC: exceptions.StorageFull(),
                      errno.EACCES: exceptions.StorageWriteDenied()}
            raise errors.get(e.errno, e)
        except Exception:
            self._delete_partial(filepath, image_id)
            raise

        checksum_hex = checksum.hexdigest()
        metadata = self._get_metadata()

        LOG.debug(_("Wrote %(bytes_written)d bytes to %(filepath)s with "
                    "checksum %(checksum_hex)s"),
                  {'bytes_written': bytes_written,
                   'filepath': filepath,
                   'checksum_hex': checksum_hex})
        return ('file://%s' % filepath, bytes_written, checksum_hex, metadata)

    @staticmethod
    def _delete_partial(filepath, iid):
        try:
            os.unlink(filepath)
        except Exception as e:
            msg = _('Unable to remove partial image data for image %s: %s')
            LOG.error(msg % (iid, e))