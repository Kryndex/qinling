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

import collections
import json
import os

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import strutils
import pecan
from pecan import rest
from webob.static import FileIter
from wsme import types as wtypes
import wsmeext.pecan as wsme_pecan

from qinling.api import access_control as acl
from qinling.api.controllers.v1 import resources
from qinling.api.controllers.v1 import types
from qinling import context
from qinling.db import api as db_api
from qinling import exceptions as exc
from qinling import rpc
from qinling.storage import base as storage_base
from qinling.utils import constants
from qinling.utils import etcd_util
from qinling.utils.openstack import keystone as keystone_util
from qinling.utils.openstack import swift as swift_util
from qinling.utils import rest_utils

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

POST_REQUIRED = set(['code'])
CODE_SOURCE = set(['package', 'swift', 'image'])
UPDATE_ALLOWED = set(['name', 'description', 'code', 'package', 'entry'])


class FunctionWorkerController(rest.RestController):
    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(resources.FunctionWorkers, types.uuid)
    def get_all(self, function_id):
        acl.enforce('function_worker:get_all', context.get_ctx())
        LOG.info("Get workers for function %s.", function_id)

        workers = etcd_util.get_workers(function_id, CONF)
        workers = [
            resources.FunctionWorker.from_dict(
                {'function_id': function_id, 'worker_name': w}
            ) for w in workers
            ]

        return resources.FunctionWorkers(workers=workers)


class FunctionsController(rest.RestController):
    workers = FunctionWorkerController()

    _custom_actions = {
        'scale_up': ['POST'],
        'scale_down': ['POST'],
        'detach': ['POST'],
    }

    def __init__(self, *args, **kwargs):
        self.storage_provider = storage_base.load_storage_provider(CONF)
        self.engine_client = rpc.get_engine_client()

        super(FunctionsController, self).__init__(*args, **kwargs)

    def _check_swift(self, container, object):
        # Auth needs to be enabled because qinling needs to check swift
        # object using user's credential.
        if not CONF.pecan.auth_enable:
            raise exc.InputException('Swift object not supported.')

        if not swift_util.check_object(container, object):
            raise exc.InputException('Object does not exist in Swift.')

    @rest_utils.wrap_pecan_controller_exception
    @pecan.expose()
    def get(self, id):
        LOG.info("Get function %s.", id)

        download = strutils.bool_from_string(
            pecan.request.GET.get('download', False)
        )
        func_db = db_api.get_function(id)
        ctx = context.get_ctx()

        if not download:
            pecan.override_template('json')
            return resources.Function.from_dict(func_db.to_dict()).to_dict()
        else:
            LOG.info("Downloading function %s", id)
            source = func_db.code['source']

            if source == constants.PACKAGE_FUNCTION:
                f = self.storage_provider.retrieve(ctx.projectid, id)
            elif source == constants.SWIFT_FUNCTION:
                container = func_db.code['swift']['container']
                obj = func_db.code['swift']['object']
                f = swift_util.download_object(container, obj)
            else:
                msg = 'Download image function is not allowed.'
                pecan.abort(
                    status_code=405,
                    detail=msg,
                    headers={'Server-Error-Message': msg}
                )

            pecan.response.app_iter = (f if isinstance(f, collections.Iterable)
                                       else FileIter(f))
            pecan.response.headers['Content-Type'] = 'application/zip'
            pecan.response.headers['Content-Disposition'] = (
                'attachment; filename="%s"' % os.path.basename(func_db.name)
            )
            LOG.info("Downloaded function %s", id)

    @rest_utils.wrap_pecan_controller_exception
    @pecan.expose('json')
    def post(self, **kwargs):
        # When using image to create function, runtime_id is not a required
        # param.
        if not POST_REQUIRED.issubset(set(kwargs.keys())):
            raise exc.InputException(
                'Required param is missing. Required: %s' % POST_REQUIRED
            )
        LOG.info("Creating function, params: %s", kwargs)

        values = {
            'name': kwargs.get('name'),
            'description': kwargs.get('description'),
            'runtime_id': kwargs.get('runtime_id'),
            'code': json.loads(kwargs['code']),
            'entry': kwargs.get('entry', 'main.main'),
        }

        source = values['code'].get('source')
        if not source or source not in CODE_SOURCE:
            raise exc.InputException(
                'Invalid code source specified, available sources: %s' %
                ', '.join(CODE_SOURCE)
            )

        if source != constants.IMAGE_FUNCTION:
            if not kwargs.get('runtime_id'):
                raise exc.InputException('"runtime_id" must be specified.')

            runtime = db_api.get_runtime(kwargs['runtime_id'])
            if runtime.status != 'available':
                raise exc.InputException(
                    'Runtime %s is not available.' % kwargs['runtime_id']
                )

        store = False
        create_trust = True
        if source == constants.PACKAGE_FUNCTION:
            store = True
            data = kwargs['package'].file.read()
        elif source == constants.SWIFT_FUNCTION:
            swift_info = values['code'].get('swift', {})
            self._check_swift(swift_info.get('container'),
                              swift_info.get('object'))
        else:
            create_trust = False
            values['entry'] = None

        if cfg.CONF.pecan.auth_enable and create_trust:
            try:
                values['trust_id'] = keystone_util.create_trust().id
                LOG.debug('Trust %s created', values['trust_id'])
            except Exception:
                raise exc.TrustFailedException(
                    'Trust creation failed for function.'
                )

        with db_api.transaction():
            func_db = db_api.create_function(values)

            if store:
                ctx = context.get_ctx()
                self.storage_provider.store(
                    ctx.projectid,
                    func_db.id,
                    data
                )

        pecan.response.status = 201
        return resources.Function.from_dict(func_db.to_dict()).to_dict()

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(resources.Functions, bool, wtypes.text)
    def get_all(self, all_projects=False, project_id=None):
        """Return a list of functions.

        :param project_id: Optional. Admin user can query other projects
            resources, the param is ignored for normal user.
        :param all_projects: Optional. Get resources of all projects.
        """
        ctx = context.get_ctx()
        if project_id and not ctx.is_admin:
            project_id = context.ctx().projectid
        if project_id and ctx.is_admin:
            all_projects = True

        if all_projects:
            acl.enforce('function:get_all:all_projects', ctx)

        filters = rest_utils.get_filters(
            project_id=project_id,
        )
        LOG.info("Get all functions. filters=%s", filters)
        db_functions = db_api.get_functions(insecure=all_projects, **filters)
        functions = [resources.Function.from_dict(db_model.to_dict())
                     for db_model in db_functions]

        return resources.Functions(functions=functions)

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(None, types.uuid, status_code=204)
    def delete(self, id):
        """Delete the specified function."""
        LOG.info("Delete function %s.", id)

        with db_api.transaction():
            func_db = db_api.get_function(id)
            if len(func_db.jobs) > 0:
                raise exc.NotAllowedException(
                    'The function is still associated with running job(s).'
                )
            if func_db.webhook:
                raise exc.NotAllowedException(
                    'The function is still associated with webhook.'
                )

            # Even admin user can not delete other project's function because
            # the trust associated can only be removed by function owner.
            if func_db.project_id != context.get_ctx().projectid:
                raise exc.NotAllowedException(
                    'Function can only be deleted by its owner.'
                )

            source = func_db.code['source']
            if source == constants.PACKAGE_FUNCTION:
                self.storage_provider.delete(func_db.project_id, id)

            # Delete all resources created by orchestrator asynchronously.
            self.engine_client.delete_function(id)

            # Delete trust if needed
            if func_db.trust_id:
                keystone_util.delete_trust(func_db.trust_id)

            # Delete etcd keys
            etcd_util.delete_function(id)

            # This will also delete function service mapping as well.
            db_api.delete_function(id)

    @rest_utils.wrap_pecan_controller_exception
    @pecan.expose('json')
    def put(self, id, **kwargs):
        """Update function.

        - Function can not being used by job.
        - Function can not being executed.
        - (TODO)Function status should be changed so no execution will create
           when function is updating.
        """
        values = {}
        for key in UPDATE_ALLOWED:
            if kwargs.get(key) is not None:
                values.update({key: kwargs[key]})

        LOG.info('Update function %s, params: %s', id, values)
        ctx = context.get_ctx()

        if set(values.keys()).issubset(set(['name', 'description'])):
            func_db = db_api.update_function(id, values)
        else:
            source = values.get('code', {}).get('source')
            with db_api.transaction():
                pre_func = db_api.get_function(id)

                if len(pre_func.jobs) > 0:
                    raise exc.NotAllowedException(
                        'The function is still associated with running job(s).'
                    )

                pre_source = pre_func.code['source']
                if source and source != pre_source:
                    raise exc.InputException(
                        "The function code type can not be changed."
                    )
                if source == constants.IMAGE_FUNCTION:
                    raise exc.InputException(
                        "The image type function code can not be changed."
                    )
                if (pre_source == constants.PACKAGE_FUNCTION and
                        values.get('package') is not None):
                    # Update the package data.
                    data = values['package'].file.read()
                    self.storage_provider.store(
                        ctx.projectid,
                        id,
                        data
                    )
                    values.pop('package')
                if pre_source == constants.SWIFT_FUNCTION:
                    swift_info = values['code'].get('swift', {})
                    self._check_swift(swift_info.get('container'),
                                      swift_info.get('object'))

                # Delete allocated resources in orchestrator and etcd keys.
                self.engine_client.delete_function(id)
                etcd_util.delete_function(id)

                func_db = db_api.update_function(id, values)

        pecan.response.status = 200
        return resources.Function.from_dict(func_db.to_dict()).to_dict()

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(
        None,
        types.uuid,
        body=resources.ScaleInfo,
        status_code=202
    )
    def scale_up(self, id, scale):
        """Scale up the containers for function execution.

        This is admin only operation. The load monitoring of function execution
        depends on the monitoring solution of underlying orchestrator.
        """
        acl.enforce('function:scale_up', context.get_ctx())

        func_db = db_api.get_function(id)
        params = scale.to_dict()

        LOG.info('Starting to scale up function %s, params: %s', id, params)

        self.engine_client.scaleup_function(
            id,
            runtime_id=func_db.runtime_id,
            count=params['count']
        )

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(
        None,
        types.uuid,
        body=resources.ScaleInfo,
        status_code=202
    )
    def scale_down(self, id, scale):
        """Scale down the containers for function execution.

        This is admin only operation. The load monitoring of function execution
        depends on the monitoring solution of underlying orchestrator.
        """
        acl.enforce('function:scale_down', context.get_ctx())

        db_api.get_function(id)
        workers = etcd_util.get_workers(id)
        params = scale.to_dict()
        if len(workers) <= 1:
            LOG.info('No need to scale down function %s', id)
            return

        LOG.info('Starting to scale down function %s, params: %s', id, params)
        self.engine_client.scaledown_function(id, count=params['count'])

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(None, types.uuid, status_code=202)
    def detach(self, id):
        """Detach the function from its underlying workers.

        This is admin only operation, which gives admin user a safe way to
        clean up the underlying resources allocated for the function.
        """
        acl.enforce('function:detach', context.get_ctx())

        db_api.get_function(id)
        LOG.info('Starting to detach function %s', id)

        # Delete allocated resources in orchestrator and etcd keys.
        self.engine_client.delete_function(id)
        etcd_util.delete_function(id)
