# Copyright 2017 Catalyst IT Limited
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import os
import zipfile

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import fileutils

from qinling import exceptions as exc
from qinling.storage import base

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class FileSystemStorage(base.PackageStorage):
    """Interact with file system for function package storage."""

    def __init__(self, *args, **kwargs):
        fileutils.ensure_tree(CONF.storage.file_system_dir)

    def store(self, project_id, function, data):
        """Store the function package data to local file system.

        :param project_id: Project ID.
        :param function: Function ID.
        :param data: Package data.
        """
        LOG.debug(
            'Store package, function: %s, project: %s', function, project_id
        )

        project_path = os.path.join(CONF.storage.file_system_dir, project_id)
        fileutils.ensure_tree(project_path)

        new_func_zip = os.path.join(project_path, '%s.zip.new' % function)
        func_zip = os.path.join(project_path, '%s.zip' % function)
        with open(new_func_zip, 'wb') as fd:
            fd.write(data)

        if not zipfile.is_zipfile(new_func_zip):
            fileutils.delete_if_exists(new_func_zip)
            raise exc.InputException("Package is not a valid ZIP package.")

        os.rename(new_func_zip, func_zip)

    def retrieve(self, project_id, function):
        """Get function package data.

        :param project_id: Project ID.
        :param function: Function ID.
        :return: File descriptor that needs to close outside.
        """
        LOG.debug(
            'Get package data, function: %s, project: %s', function, project_id
        )

        func_zip = os.path.join(
            CONF.storage.file_system_dir,
            '%s/%s.zip' % (project_id, function)
        )

        if not os.path.exists(func_zip):
            raise exc.StorageNotFoundException(
                'Package of function %s for project %s not found.' %
                (function, project_id)
            )

        f = open(func_zip, 'rb')
        LOG.debug('Found package data')

        return f

    def delete(self, project_id, function):
        LOG.debug(
            'Delete package data, function: %s, project: %s', function,
            project_id
        )

        func_zip = os.path.join(
            CONF.storage.file_system_dir,
            '%s/%s.zip' % (project_id, function)
        )

        if os.path.exists(func_zip):
            os.remove(func_zip)
