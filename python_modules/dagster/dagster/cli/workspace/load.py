import os
import warnings

import six

from dagster import check
from dagster.api.list_repositories import (
    sync_list_repositories,
    sync_list_repositories_ephemeral_grpc,
)
from dagster.core.code_pointer import CodePointer, rebase_file
from dagster.core.definitions.reconstructable import def_from_pointer
from dagster.core.errors import DagsterInvariantViolationError
from dagster.core.host_representation import RepositoryLocationApi, RepositoryLocationHandle
from dagster.utils import load_yaml_from_path

from .autodiscovery import (
    LoadableTarget,
    loadable_targets_from_python_file,
    loadable_targets_from_python_module,
    loadable_targets_from_python_package,
)
from .config_schema import ensure_workspace_config
from .workspace import Workspace


def load_workspace_from_yaml_paths(yaml_paths):
    check.list_param(yaml_paths, 'yaml_paths', str)

    workspace_configs = [load_yaml_from_path(yaml_path) for yaml_path in yaml_paths]
    workspaces = [
        load_workspace_from_config(workspace_config, yaml_path)
        for workspace_config, yaml_path in zip(workspace_configs, yaml_paths)
    ]

    repository_location_handles_dict = {}
    for workspace in workspaces:
        for repository_location_name in workspace.repository_location_names:
            repository_location_handles_dict[
                repository_location_name
            ] = workspace.get_repository_location_handle(repository_location_name)

    repository_location_handles = list(repository_location_handles_dict.values())
    merged_workspace = Workspace(repository_location_handles)
    return merged_workspace


def load_workspace_from_config(workspace_config, yaml_path):
    ensure_workspace_config(workspace_config, yaml_path)

    if 'repository' in workspace_config:
        warnings.warn(
            # link to docs once they exist
            'You are using the legacy repository yaml format. Please update your file '
            'to abide by the new workspace file format.'
        )
        return Workspace(
            [
                RepositoryLocationHandle.create_in_process_location(
                    pointer=CodePointer.from_legacy_repository_yaml(yaml_path)
                )
            ]
        )

    opt_ins = set(workspace_config.get('opt_in') if workspace_config.get('opt_in') else [])

    location_handles = []
    for location_config in workspace_config['load_from']:
        location_handles.append(
            _location_handle_from_location_config(location_config, yaml_path, opt_ins)
        )

    return Workspace(location_handles)


def load_def_in_module(module_name, attribute):
    return def_from_pointer(CodePointer.from_module(module_name, attribute))


def load_def_in_package(package_name, attribute):
    return def_from_pointer(CodePointer.from_python_package(package_name, attribute))


def load_def_in_python_file(python_file, attribute, working_directory):
    return def_from_pointer(CodePointer.from_python_file(python_file, attribute, working_directory))


def _location_handle_from_module_config(python_module_config, api):
    check.inst_param(api, 'api', RepositoryLocationApi)
    module_name, attribute, location_name = _get_module_config_data(python_module_config)
    return location_handle_from_module_name(module_name, attribute, api, location_name)


def _get_module_config_data(python_module_config):
    return (
        (python_module_config, None, None)
        if isinstance(python_module_config, six.string_types)
        else (
            python_module_config['module_name'],
            python_module_config.get('attribute'),
            python_module_config.get('location_name'),
        )
    )


def location_handle_from_module_name(module_name, attribute, api, location_name=None):
    check.str_param(module_name, 'module_name')
    check.opt_str_param(attribute, 'attribute')
    check.inst_param(api, 'api', RepositoryLocationApi)
    check.opt_str_param(location_name, 'location_name')

    loadable_targets = (
        [LoadableTarget(attribute, load_def_in_module(module_name, attribute))]
        if attribute
        else loadable_targets_from_python_module(module_name)
    )

    repository_code_pointer_dict = {}
    for loadable_target in loadable_targets:
        repository_code_pointer_dict[
            loadable_target.target_definition.name
        ] = CodePointer.from_module(module_name, loadable_target.attribute)

    return RepositoryLocationHandle.create_out_of_process_location(
        repository_code_pointer_dict=repository_code_pointer_dict,
        # default to the name of the repository symbol for now
        location_name=assign_location_name(location_name, repository_code_pointer_dict),
        api=api,
    )


def _location_handle_from_package_config(python_module_config, api):
    check.inst_param(api, 'api', RepositoryLocationApi)
    module_name, attribute, location_name = _get_package_config_data(python_module_config)
    return location_handle_from_package_name(module_name, attribute, api, location_name)


def _get_package_config_data(python_package_config):
    return (
        (python_package_config, None, None)
        if isinstance(python_package_config, six.string_types)
        else (
            python_package_config['package_name'],
            python_package_config.get('attribute'),
            python_package_config.get('location_name'),
        )
    )


def location_handle_from_package_name(package_name, attribute, api, location_name=None):
    check.str_param(package_name, 'package_name')
    check.opt_str_param(attribute, 'attribute')
    check.inst_param(api, 'api', RepositoryLocationApi)
    check.opt_str_param(location_name, 'location_name')

    loadable_targets = (
        [LoadableTarget(attribute, load_def_in_package(package_name, attribute))]
        if attribute
        else loadable_targets_from_python_package(package_name)
    )

    repository_code_pointer_dict = {}
    for loadable_target in loadable_targets:
        repository_code_pointer_dict[
            loadable_target.target_definition.name
        ] = CodePointer.from_python_package(package_name, loadable_target.attribute)

    return RepositoryLocationHandle.create_out_of_process_location(
        repository_code_pointer_dict=repository_code_pointer_dict,
        # default to the name of the repository symbol for now
        location_name=assign_location_name(location_name, repository_code_pointer_dict),
    )


def assign_location_name(location_name, repository_code_pointer_dict):
    if location_name:
        return location_name

    if len(repository_code_pointer_dict) > 1:
        raise DagsterInvariantViolationError(
            'If there is one than more repository you must provide a location name'
        )

    return next(iter(repository_code_pointer_dict.keys()))


def location_handle_from_python_file_config(python_file_config, yaml_path, api):
    check.str_param(yaml_path, 'yaml_path')
    check.inst_param(api, 'api', RepositoryLocationApi)

    absolute_path, attribute, location_name, working_directory = _get_python_file_config_data(
        python_file_config, yaml_path
    )

    return location_handle_from_python_file(
        absolute_path, attribute, api, location_name, working_directory
    )


def _get_python_file_config_data(python_file_config, yaml_path):
    return (
        (rebase_file(python_file_config, yaml_path), None, None, None)
        if isinstance(python_file_config, six.string_types)
        else (
            rebase_file(python_file_config['relative_path'], yaml_path),
            python_file_config.get('attribute'),
            python_file_config.get('location_name'),
            rebase_file(python_file_config.get('working_directory'), yaml_path)
            if python_file_config.get('working_directory')
            else None,
        )
    )


def location_handle_from_python_file(
    python_file, attribute, api, location_name=None, working_directory=None
):
    check.str_param(python_file, 'python_file')
    check.opt_str_param(attribute, 'attribute')
    check.inst_param(api, 'api', RepositoryLocationApi)
    check.opt_str_param(location_name, 'location_name')
    check.opt_str_param(working_directory, 'working_directory')

    loadable_targets = (
        [
            LoadableTarget(
                attribute, load_def_in_python_file(python_file, attribute, working_directory)
            )
        ]
        if attribute
        else loadable_targets_from_python_file(python_file, working_directory)
    )

    repository_code_pointer_dict = {}
    for loadable_target in loadable_targets:
        repository_code_pointer_dict[
            loadable_target.target_definition.name
        ] = CodePointer.from_python_file(python_file, loadable_target.attribute, working_directory)

    return RepositoryLocationHandle.create_out_of_process_location(
        repository_code_pointer_dict=repository_code_pointer_dict,
        # default to the name of the repository symbol for now
        location_name=assign_location_name(location_name, repository_code_pointer_dict),
        api=api,
    )


def _list_repositories(executable_path, python_file, module_name, working_directory, api):
    check.str_param(executable_path, 'executable_path')
    check.opt_str_param(python_file, 'python_file')
    check.opt_str_param(module_name, 'module_name')
    check.opt_str_param(working_directory, 'working_directory')
    check.inst_param(api, 'api', RepositoryLocationApi)

    if api == RepositoryLocationApi.GRPC:
        return sync_list_repositories_ephemeral_grpc(
            executable_path=executable_path,
            python_file=python_file,
            module_name=module_name,
            working_directory=working_directory,
        )
    else:
        return sync_list_repositories(
            executable_path=executable_path,
            python_file=python_file,
            module_name=module_name,
            working_directory=working_directory,
        )


def _assign_grpc_location_name(port, socket, host):
    check.opt_int_param(port, 'port')
    check.opt_str_param(socket, 'socket')
    check.str_param(host, 'host')
    check.invariant(port or socket)
    return 'grpc:{host}:{socket_or_port}'.format(
        host=host, socket_or_port=(socket if socket else port)
    )


def _location_handle_from_grpc_server_config(grpc_server_config, yaml_path):
    check.dict_param(grpc_server_config, 'grpc_server_config')
    check.str_param(yaml_path, 'yaml_path')

    port, socket, host, location_name = (
        grpc_server_config.get('port'),
        grpc_server_config.get('socket'),
        grpc_server_config.get('host'),
        grpc_server_config.get('location_name'),
    )

    check.invariant(
        (socket or port) and not (socket and port), "must supply either a socket or a port"
    )

    if not host:
        host = 'localhost'

    if not location_name:
        location_name = _assign_grpc_location_name(port, socket, host)

    return RepositoryLocationHandle.create_grpc_server_location(
        port=port, socket=socket, host=host, location_name=location_name,
    )


def _location_handle_from_python_environment_config(python_environment_config, yaml_path, api):
    check.dict_param(python_environment_config, 'python_environment_config')
    check.str_param(yaml_path, 'yaml_path')
    check.inst_param(api, 'api', RepositoryLocationApi)

    executable_path, target_config = (
        # do shell expansion on path
        os.path.expanduser(python_environment_config['executable_path']),
        python_environment_config['target'],
    )

    check.invariant(is_target_config(target_config))

    python_file_config, python_module_config, python_package_config = (
        target_config.get('python_file'),
        target_config.get('python_module'),
        target_config.get('python_package'),
    )

    if python_file_config:
        absolute_path, attribute, location_name, working_directory = _get_python_file_config_data(
            python_file_config, yaml_path
        )
        if not attribute:
            response = _list_repositories(
                executable_path=executable_path,
                python_file=absolute_path,
                module_name=None,
                working_directory=None,
                api=api,
            )
            return RepositoryLocationHandle.create_python_env_location(
                executable_path=executable_path,
                location_name=location_name,
                repository_code_pointer_dict={
                    lrs.attribute: CodePointer.from_python_file(
                        absolute_path, lrs.attribute, working_directory
                    )
                    for lrs in response.repository_symbols
                },
                api=api,
            )
        else:
            return RepositoryLocationHandle.create_python_env_location(
                executable_path=executable_path,
                location_name=location_name,
                repository_code_pointer_dict={
                    attribute: CodePointer.from_python_file(
                        absolute_path, attribute, working_directory
                    )
                },
                api=api,
            )

    elif python_module_config:
        check.invariant(python_module_config)
        module_name, attribute, location_name = _get_module_config_data(python_module_config)

        if not attribute:
            response = _list_repositories(
                executable_path=executable_path,
                python_file=None,
                module_name=module_name,
                working_directory=None,
                api=api,
            )
            return RepositoryLocationHandle.create_python_env_location(
                executable_path=executable_path,
                location_name=location_name,
                repository_code_pointer_dict={
                    lrs.attribute: CodePointer.from_module(module_name, lrs.attribute)
                    for lrs in response.repository_symbols
                },
                api=api,
            )
        else:
            return RepositoryLocationHandle.create_python_env_location(
                executable_path=executable_path,
                location_name=location_name,
                repository_code_pointer_dict={
                    attribute: CodePointer.from_module(module_name, attribute)
                },
                api=api,
            )

    else:
        check.invariant(python_package_config)
        package_name, attribute, location_name = _get_package_config_data(python_package_config)

        if not attribute:
            response = _list_repositories(
                executable_path=executable_path,
                python_file=None,
                module_name=package_name,
                working_directory=None,
                api=api,
            )
            return RepositoryLocationHandle.create_python_env_location(
                executable_path=executable_path,
                location_name=location_name,
                repository_code_pointer_dict={
                    lrs.attribute: CodePointer.from_python_package(package_name, lrs.attribute)
                    for lrs in response.repository_symbols
                },
                api=api,
            )
        else:
            return RepositoryLocationHandle.create_python_env_location(
                executable_path=executable_path,
                location_name=location_name,
                repository_code_pointer_dict={
                    attribute: CodePointer.from_python_package(package_name, attribute)
                },
                api=api,
            )


def _location_handle_from_location_config(location_config, yaml_path, opt_ins):
    check.dict_param(location_config, 'location_config')
    check.str_param(yaml_path, 'yaml_path')
    check.set_param(opt_ins, 'opt_ins')

    api = RepositoryLocationApi.GRPC if 'grpc' in opt_ins else RepositoryLocationApi.CLI

    if is_target_config(location_config):
        return _location_handle_from_target_config(location_config, yaml_path, api)

    elif 'grpc_server' in location_config:
        return _location_handle_from_grpc_server_config(location_config['grpc_server'], yaml_path)

    elif 'python_environment' in location_config:
        return _location_handle_from_python_environment_config(
            location_config['python_environment'], yaml_path, api
        )

    else:
        check.not_implemented('Unsupported location config: {}'.format(location_config))


def is_target_config(potential_target_config):
    return isinstance(potential_target_config, dict) and (
        potential_target_config.get('python_file')
        or potential_target_config.get('python_module')
        or potential_target_config.get('python_package')
    )


def _location_handle_from_target_config(target_config, yaml_path, api):
    check.dict_param(target_config, 'target_config')
    check.param_invariant(is_target_config(target_config), 'target_config')
    check.str_param(yaml_path, 'yaml_path')
    check.inst_param(api, 'api', RepositoryLocationApi)

    if 'python_file' in target_config:
        return location_handle_from_python_file_config(target_config['python_file'], yaml_path, api)

    elif 'python_module' in target_config:
        return _location_handle_from_module_config(target_config['python_module'], api)

    elif 'python_package' in target_config:
        return _location_handle_from_package_config(target_config['python_package'], api)

    else:
        check.failed('invalid target_config')
