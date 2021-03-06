"""Deploy module for chalice apps.

Handles Lambda and API Gateway deployments.

"""
import os
import sys
import uuid
import shutil
import json
import subprocess
import zipfile
import hashlib
import inspect
import re

from typing import Any, Tuple, Callable, IO  # noqa
import botocore.session  # noqa
import virtualenv

import chalice
from chalice import app
from chalice import policy
from chalice.config import Config  # noqa
from chalice.awsclient import TypedAWSClient
from chalice import compat

LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "",
        "Effect": "Allow",
        "Principal": {
            "Service": "lambda.amazonaws.com"
        },
        "Action": "sts:AssumeRole"}
    ]
}

CLOUDWATCH_LOGS = {
    "Effect": "Allow",
    "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
    ],
    "Resource": "arn:aws:logs:*:*:*"
}

FULL_PASSTHROUGH = """
#set($allParams = $input.params())
{
"body-json" : $input.json('$'),
"base64-body": "$util.base64Encode($input.body)",
"params" : {
#foreach($type in $allParams.keySet())
  #set($params = $allParams.get($type))
"$type" : {
  #foreach($paramName in $params.keySet())
  "$paramName" : "$util.escapeJavaScript($params.get($paramName))"
      #if($foreach.hasNext),#end
  #end
}
  #if($foreach.hasNext),#end
#end
},
"stage-variables" : {
#foreach($key in $stageVariables.keySet())
"$key" : "$util.escapeJavaScript($stageVariables.get($key))"
  #if($foreach.hasNext),#end
#end
},
"context" : {
  "account-id": "$context.identity.accountId",
  "api-id": "$context.apiId",
  "api-key": "$context.identity.apiKey",
  "authorizer-principal-id": "$context.authorizer.principalId",
  "caller": "$context.identity.caller",
  "cognito-authentication-provider": \
    "$context.identity.cognitoAuthenticationProvider",
  "cognito-authentication-type": "$context.identity.cognitoAuthenticationType",
  "cognito-identity-id": "$context.identity.cognitoIdentityId",
  "cognito-identity-pool-id": "$context.identity.cognitoIdentityPoolId",
  "http-method": "$context.httpMethod",
  "stage": "$context.stage",
  "source-ip": "$context.identity.sourceIp",
  "user": "$context.identity.user",
  "user-agent": "$context.identity.userAgent",
  "user-arn": "$context.identity.userArn",
  "request-id": "$context.requestId",
  "resource-id": "$context.resourceId",
  "resource-path": "$context.resourcePath"
  }
}
"""

ERROR_MAPPING = (
    "#set($inputRoot = $input.path('$'))"
    "{"
    '"Code": "$inputRoot.errorType",'
    '"Message": "$inputRoot.errorMessage"'
    "}"
)

NULLARY = Callable[[], str]


def create_default_deployer(session, prompter=None):
    # type: (botocore.session.Session, NoPrompt) -> Deployer
    if prompter is None:
        prompter = NoPrompt()
    aws_client = TypedAWSClient(session)
    api_gateway_deploy = APIGatewayDeployer(
        aws_client, session.create_client('apigateway'),
        session.create_client('lambda'))

    packager = LambdaDeploymentPackager()
    osutils = OSUtils()
    lambda_deploy = LambdaDeployer(
        aws_client, packager, prompter, osutils)
    return Deployer(api_gateway_deploy, lambda_deploy)


def build_url_trie(routes):
    # type: (Dict[str, app.RouteEntry]) -> Dict[str, Any]
    """Create a URL trie based on request routes.

    :type routes: dict
    :param routes: A dict of routes.  Keys are the uri_pattern,
        values are the ``chalice.app.RouteEntry`` values.

    :rtype: dict
    :return: A prefix trie of URL patterns.

    """
    _validate_routes(routes)
    root = node('', '/')
    for route in routes:
        if route == '/':
            # '/foo'.split('/') == ['', 'foo']
            # '/foo/bar'.split('/') == ['', 'foo', 'bar']
            # '/'.split('/') == ['', ''] <----???
            # So we special case this to return what split('/')
            # should return for "/".
            parts = ['']
        else:
            parts = route.split('/')
        current = root
        for i, part in enumerate(parts):
            if part not in current['children']:
                if part == '':
                    uri_path = '/'
                else:
                    uri_path = '/'.join(parts[:i + 1])
                current['children'][part] = node(part, uri_path)
            current = current['children'][part]
        current['is_route'] = True
        current['route_entry'] = routes[route]
    return root['children'].values()[0]


def validate_configuration(config):
    # type: (Config) -> None
    """Validate app configuration.

    The purpose of this method is to provide a fail fast mechanism
    for anything we know is going to fail deployment.
    We can detect common error cases and provide the user with helpful
    error messages.

    """
    routes = config.chalice_app.routes
    _validate_routes(routes)
    _validate_manage_iam_role(config)


def _validate_routes(routes):
    # type: (Dict[str, Any]) -> None
    # We're trying to validate any kind of route that will fail
    # when we send the request to API gateway.
    # We check for:
    #
    # * any routes that end with a trailing slash.
    for route in routes:
        if route != '/' and route.endswith('/'):
            raise ValueError("Route cannot end with a trailing slash: %s"
                             % route)


def _validate_manage_iam_role(config):
    # type: (Config) -> None
    # We need to check if manage_iam_role is None because that's the value
    # it the user hasn't specified this value.
    # However, if the manage_iam_role value is not None, the user set it
    # to something, in which case we care if they set it to False.
    if not config.manage_iam_role:
        # If they don't want us to manage the role, they
        # have to specify an iam_role_arn.
        if not config.iam_role_arn:
            raise ValueError(
                "When 'manage_iam_role' is set to false, you "
                "must provide an 'iam_role_arn' in config.json."
            )


def node(name, uri_path, is_route=False):
    # type: (str, str, bool) -> Dict[str, Any]
    return {
        'name': name,
        'uri_path': uri_path,
        'children': {},
        'resource_id': None,
        'parent_resource_id': None,
        'is_route': is_route,
        'route_entry': None,
    }


class NoPrompt(object):
    def confirm(self, text, default=False, abort=False):
        # type: (str, bool, bool) -> bool
        return default


class Deployer(object):
    def __init__(self, apigateway_deploy, lambda_deploy):
        # type: (APIGatewayDeployer, LambdaDeployer) -> None
        self._apigateway_deploy = apigateway_deploy
        self._lambda_deploy = lambda_deploy

    def deploy(self, config):
        # type: (Config) -> Tuple[str, str, str]
        """Deploy chalice application to AWS.

        :type config: dict
        :param config: A dictionary of config values including:

            * project_dir - The directory containing the project
            * config - A dictionary of config values loaded from the
                project config file.

        """
        validate_configuration(config)
        self._lambda_deploy.deploy(config)
        rest_api_id, region_name, stage = self._apigateway_deploy.deploy(
            config)
        print (
            "https://{api_id}.execute-api.{region}.amazonaws.com/{stage}/"
            .format(api_id=rest_api_id, region=region_name, stage=stage)
        )
        return rest_api_id, region_name, stage


class APIGatewayResourceCreator(object):
    """Create hierarchical resources in API gateway from chalice routes."""

    def __init__(self, awsclient, apig_methods, lambda_arn,
                 random_id_generator=lambda: str(uuid.uuid4())):
        # type: (TypedAWSClient, APIGatewayMethods, str, NULLARY) -> None
        self.awsclient = awsclient
        self._apig_methods = apig_methods
        self.region_name = self.awsclient.region_name
        self.rest_api_id = apig_methods.rest_api_id
        self.lambda_arn = lambda_arn
        self._random_id = random_id_generator

    def build_resources(self, chalice_trie):
        # type: (Dict[str, Any]) -> None
        """Create API gateway resources from chalice routes.

        :type chalice_trie: dict
        :param chalice_trie: The trie of URLs from ``build_url_trie()``.

        """
        # We need to create the parent resource before we can create
        # child resources, so we'll do a pre-order depth first traversal.
        stack = [chalice_trie]
        while stack:
            current = stack.pop()
            # If there's no resource_id we need to create it.
            if current['resource_id'] is None:
                assert current['parent_resource_id'] is not None, current
                response = self.awsclient.create_rest_resource(
                    self.rest_api_id, current['parent_resource_id'],
                    current['name']
                )
                new_resource_id = response['id']
                current['resource_id'] = new_resource_id
                for child in current['children']:
                    current['children'][child]['parent_resource_id'] = \
                        new_resource_id
            if current['is_route']:
                assert current['route_entry'] is not None, current
                for http_method in current['route_entry'].methods:
                    self._configure_resource_route(current, http_method)
                if current['route_entry'].cors:
                    self._apig_methods.create_preflight_method(
                        current['resource_id'], current['route_entry'].methods)
            for child in current['children']:
                stack.append(current['children'][child])
        # Add a catch all auth that says anything in this rest API can call
        # the lambda function.
        self.awsclient.add_permission_for_apigateway_if_needed(
            self.lambda_arn.split(':')[-1],
            self.region_name,
            self.lambda_arn.split(':')[4],
            self.rest_api_id,
            self._random_id(),
        )

    def _camel_convert(self, name):
        # type: (str) -> str
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    def _configure_resource_route(self, node, http_method):
        # type: (Dict[str, Any], str) -> None
        route_entry = node['route_entry']
        resource_id = node['resource_id']
        self._apig_methods.create_method_request(
            resource_id, http_method, route_entry.authorization_type,
            route_entry.authorizer_id, route_entry.api_key_required,
        )
        self._apig_methods.create_lambda_method_integration(
            resource_id, http_method, route_entry.content_types,
            self._lambda_uri()
        )
        self._apig_methods.create_method_response(
            resource_id, http_method, route_entry.cors)
        self._apig_methods.create_integration_response(
            resource_id, http_method, route_entry.cors)
        self._apig_methods.create_error_responses(
            resource_id, http_method, app.ALL_ERRORS, route_entry.cors)

    def _lambda_uri(self):
        # type: () -> str
        region_name = self.region_name
        api_version = '2015-03-31'
        return (
            "arn:aws:apigateway:{region_name}:lambda:path/{api_version}"
            "/functions/{lambda_arn}/invocations".format(
                region_name=region_name,
                api_version=api_version,
                lambda_arn=self.lambda_arn)
        )


class LambdaDeploymentPackager(object):
    def _create_virtualenv(self, venv_dir):
        # type: (str) -> None
        # The original implementation used Popen(['virtualenv', ...])
        # However, it's hard to make assumptions about how a users
        # PATH is set up.  This could result in using old versions
        # of virtualenv that give confusing error messages.
        # To fix this issue, we're calling directly into the
        # virtualenv package.  The main() method doesn't accept
        # args, so we need to patch out sys.argv with the venv
        # dir.  The original sys.argv is replaced on exit.
        original = sys.argv
        sys.argv = ['', venv_dir, '--quiet']
        try:
            virtualenv.main()
        finally:
            sys.argv = original

    def create_deployment_package(self, project_dir):
        # type: (str) -> str
        print "Creating deployment package."
        # pip install -t doesn't work out of the box with homebrew and
        # python, so we're using virtualenvs instead which works in
        # more cases.
        venv_dir = os.path.join(project_dir, '.chalice', 'venv')
        self._create_virtualenv(venv_dir)
        pip_exe = compat.pip_script_in_venv(venv_dir)
        assert os.path.isfile(pip_exe)
        # Next install any requirements specified by the app.
        requirements_file = os.path.join(project_dir, 'requirements.txt')
        deployment_package_filename = self.deployment_package_filename(
            project_dir)
        if self._has_at_least_one_package(requirements_file) and not \
                os.path.isfile(deployment_package_filename):
            p = subprocess.Popen([pip_exe, 'install', '-r', requirements_file],
                                 stdout=subprocess.PIPE)
            p.communicate()
        deps_dir = compat.site_packages_dir_in_venv(venv_dir)
        assert os.path.isdir(deps_dir)
        # Now we need to create a zip file and add in the site-packages
        # dir first, followed by the app_dir contents next.
        if not os.path.isdir(os.path.dirname(deployment_package_filename)):
            os.makedirs(os.path.dirname(deployment_package_filename))
        with zipfile.ZipFile(deployment_package_filename, 'w',
                             compression=zipfile.ZIP_DEFLATED) as z:
            self._add_py_deps(z, deps_dir)
            self._add_app_files(z, project_dir)
        return deployment_package_filename

    def _has_at_least_one_package(self, filename):
        # type: (str) -> bool
        if not os.path.isfile(filename):
            return False
        with open(filename, 'r') as f:
            # This is meant to be a best effort attempt.
            # This can return True and still have no packages
            # actually being specified, but those aren't common
            # cases.
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    return True
        return False

    def deployment_package_filename(self, project_dir):
        # type: (str) -> str
        # Computes the name of the deployment package zipfile
        # based on a hash of the requirements file.
        # This is done so that we only "pip install -r requirements.txt"
        # when we know there's new dependencies we need to install.
        requirements_file = os.path.join(project_dir, 'requirements.txt')
        hash_contents = self._hash_requirements_file(requirements_file)
        deployment_package_filename = os.path.join(
            project_dir, '.chalice', 'deployments', hash_contents + '.zip')
        return deployment_package_filename

    def _add_py_deps(self, zip, deps_dir):
        # type: (zipfile.ZipFile, str) -> None
        prefix_len = len(deps_dir) + 1
        for root, dirnames, filenames in os.walk(deps_dir):
            if root == deps_dir and 'chalice' in dirnames:
                # Don't include any chalice deps.  We cherry pick
                # what we want to include in _add_app_files.
                dirnames.remove('chalice')
            for filename in filenames:
                full_path = os.path.join(root, filename)
                zip_path = full_path[prefix_len:]
                zip.write(full_path, zip_path)

    def _add_app_files(self, zip, project_dir):
        # type: (zipfile.ZipFile, str) -> None
        # TODO: This will need to change in the future, but
        # for now we're just supporting an app.py file.
        chalice_router = inspect.getfile(app)
        if chalice_router.endswith('.pyc'):
            chalice_router = chalice_router[:-1]
        zip.write(chalice_router, 'chalice/app.py')

        chalice_init = inspect.getfile(chalice)
        if chalice_init.endswith('.pyc'):
            chalice_init = chalice_init[:-1]
        zip.write(chalice_init, 'chalice/__init__.py')

        zip.write(os.path.join(project_dir, 'app.py'),
                  'app.py')

    def _hash_requirements_file(self, filename):
        # type: (str) -> str
        if not os.path.isfile(filename):
            contents = ''
        else:
            with open(filename) as f:
                contents = f.read()
        return hashlib.md5(contents).hexdigest()

    def inject_latest_app(self, deployment_package_filename, project_dir):
        # type: (str, str) -> None
        """Inject latest version of chalice app into a zip package.

        This method takes a pre-created deployment package and injects
        in the latest chalice app code.  This is useful in the case where
        you have no new package deps but have updated your chalice app code.

        :type deployment_package_filename: str
        :param deployment_package_filename: The zipfile of the
            preexisting deployment package.

        :type project_dir: str
        :param project_dir: Path to chalice project dir.

        """
        # Use the premade zip file and replace the app.py file
        # with the latest version.  Python's zipfile does not have
        # a way to do this efficiently so we need to create a new
        # zip file that has all the same stuff except for the new
        # app file.
        # TODO: support more than just an app.py file.
        print "Regen deployment package..."
        tmpzip = deployment_package_filename + '.tmp.zip'
        with zipfile.ZipFile(deployment_package_filename, 'r') as inzip:
            with zipfile.ZipFile(tmpzip, 'w') as outzip:
                for el in inzip.infolist():
                    if el.filename == 'app.py':
                        continue
                    else:
                        contents = inzip.read(el.filename)
                        outzip.writestr(el, contents)
                # Then at the end, add back the app.py.
                app_py = os.path.join(project_dir, 'app.py')
                assert os.path.isfile(app_py), app_py
                outzip.write(app_py, 'app.py')
        shutil.move(tmpzip, deployment_package_filename)


class LambdaDeployer(object):
    def __init__(self,
                 aws_client,   # type: TypedAWSClient
                 packager,     # type: LambdaDeploymentPackager
                 prompter,     # type: NoPrompt
                 osutils,      # type: OSUtils
                 ):
        # type: (...) -> None
        self._aws_client = aws_client
        self._packager = packager
        self._prompter = prompter
        self._osutils = osutils

    def deploy(self, config):
        # type: (Config) -> None
        app_name = config.app_name
        if self._aws_client.lambda_function_exists(app_name):
            self._get_or_create_lambda_role_arn(config)
            self._update_lambda_function(config)
        else:
            function_arn = self._first_time_lambda_create(config)
            # Record the lambda_arn for later use.
            config.config_from_disk['lambda_arn'] = function_arn
            self._write_config_to_disk(config)
        print "Lambda deploy done."

    def _get_or_create_lambda_role_arn(self, config):
        # type: (Config) -> str
        if not config.manage_iam_role:
            # We've already validated the config, so we know
            # if manage_iam_role==False, then they've provided a
            # an iam_role_arn.
            return config.iam_role_arn

        app_name = config.app_name
        try:
            role_arn = self._aws_client.get_role_arn_for_name(app_name)
            self._update_role_with_latest_policy(app_name, config)
        except ValueError:
            print "Creating role"
            role_arn = self._create_role_from_source_code(config)
        return role_arn

    def _update_role_with_latest_policy(self, app_name, config):
        # type: (str, Config) -> None
        print "Updating IAM policy."
        app_policy = self._get_policy_from_source_code(config)
        previous = self._load_last_policy(config)
        diff = policy.diff_policies(previous, app_policy)
        if diff:
            if diff.get('added', set([])):
                print ("\nThe following actions will be added to "
                       "the execution policy:\n")
                for action in diff['added']:
                    print action
            if diff.get('removed', set([])):
                print ("\nThe following action will be removed from "
                       "the execution policy:\n")
                for action in diff['removed']:
                    print action
            self._prompter.confirm("\nWould you like to continue? ",
                                   default=True, abort=True)
        self._aws_client.delete_role_policy(
            role_name=app_name, policy_name=app_name)
        self._aws_client.put_role_policy(role_name=app_name,
                                         policy_name=app_name,
                                         policy_document=app_policy)
        self._record_policy(config, app_policy)

    def _get_policy_from_source_code(self, config):
        # type: (Config) -> Dict[str, Any]
        if config.autogen_policy:
            app_py = os.path.join(config.project_dir, 'app.py')
            assert os.path.isfile(app_py)
            with open(app_py) as f:
                app_policy = policy.policy_from_source_code(f.read())
                app_policy['Statement'].append(CLOUDWATCH_LOGS)
                return app_policy
        else:
            app_policy = self._load_last_policy(config)
            return app_policy

    def _load_last_policy(self, config):
        # type: (Config) -> Dict[str, Any]
        policy_file = os.path.join(config.project_dir,
                                   '.chalice', 'policy.json')
        if not os.path.isfile(policy_file):
            # TODO: Should return at least {'Statement': []}.
            return {}
        with open(policy_file, 'r') as f:
            return json.loads(f.read())

    def _record_policy(self, config, policy):
        # type: (Config, Dict[str, Any]) -> None
        policy_file = os.path.join(config.project_dir,
                                   '.chalice', 'policy.json')
        with open(policy_file, 'w') as f:
            f.write(json.dumps(policy, indent=2, separators=(',', ': ')))

    def _first_time_lambda_create(self, config):
        # type: (Config) -> str
        # Creates a lambda function and returns the
        # function arn.
        # First we need to create a deployment package.
        print "Initial creation of lambda function."
        app_name = config.app_name
        role_arn = self._get_or_create_lambda_role_arn(config)
        zip_filename = self._packager.create_deployment_package(
            config.project_dir)
        with open(zip_filename, 'rb') as f:
            zip_contents = f.read()
        return self._aws_client.create_function(
            app_name, role_arn, zip_contents)

    def _update_lambda_function(self, config):
        # type: (Config) -> None
        print "Updating lambda function..."
        project_dir = config.project_dir
        packager = self._packager
        deployment_package_filename = packager.deployment_package_filename(
            project_dir)
        if self._osutils.file_exists(deployment_package_filename):
            packager.inject_latest_app(deployment_package_filename,
                                       project_dir)
        else:
            deployment_package_filename = packager.create_deployment_package(
                project_dir)
        zip_contents = self._osutils.get_file_contents(
            deployment_package_filename, binary=True)
        print "Sending changes to lambda."
        self._aws_client.update_function_code(config.app_name,
                                              zip_contents)

    def _write_config_to_disk(self, config):
        # type: (Config) -> None
        config_filename = os.path.join(config.project_dir,
                                       '.chalice', 'config.json')
        with open(config_filename, 'w') as f:
            f.write(json.dumps(config.config_from_disk, indent=2))

    def _create_role_from_source_code(self, config):
        # type: (Config) -> str
        app_name = config.app_name
        app_policy = self._get_policy_from_source_code(config)
        if len(app_policy['Statement']) > 1:
            print "The following execution policy will be used:"
            print json.dumps(app_policy, indent=2)
            self._prompter.confirm("Would you like to continue? ",
                                   default=True, abort=True)
        role_arn = self._aws_client.create_role(
            name=app_name,
            trust_policy=LAMBDA_TRUST_POLICY,
            policy=app_policy
        )
        self._record_policy(config, app_policy)
        return role_arn


class APIGatewayDeployer(object):
    def __init__(self,
                 aws_client,          # type: TypedAWSClient
                 api_gateway_client,  # type: Any
                 lambda_client        # type: Any
                 ):
        # type: (...) -> None
        self._aws_client = aws_client
        self._api_gateway_client = api_gateway_client
        self._lambda_client = lambda_client

    def deploy(self, config):
        # type: (Config) -> Tuple[str, str, str]
        # Perhaps move this into APIGatewayResourceCreator.
        app_name = config.app_name
        rest_api_id = self._aws_client.get_rest_api_id(app_name)
        if rest_api_id is None:
            print "Initiating first time deployment..."
            return self._first_time_deploy(config)
        else:
            print "API Gateway rest API already found."
            self._remove_all_resources(rest_api_id)
            return self._create_resources_for_api(config, rest_api_id)

    def _remove_all_resources(self, rest_api_id):
        # type: (str) -> None
        all_resources = self._aws_client.get_resources_for_api(rest_api_id)
        first_tier_ids = [r['id'] for r in all_resources
                          if r['path'].count('/') == 1 and r['path'] != '/']
        print "Deleting root resource id"
        for resource_id in first_tier_ids:
            self._aws_client.delete_resource_for_api(rest_api_id, resource_id)
        root_resource = [r for r in all_resources if r['path'] == '/'][0]
        # We can't delete the root resource, but we need to remove all the
        # existing methods otherwise we'll get 4xx from API gateway when we
        # try to add methods to the root resource on a redeploy.
        self._aws_client.delete_methods_from_root_resource(
            rest_api_id, root_resource)
        print "Done deleting existing resources."

    def _lambda_uri(self, lambda_function_arn):
        # type: (str) -> str
        region_name = self._aws_client.region_name
        api_version = '2015-03-31'
        return (
            "arn:aws:apigateway:{region_name}:lambda:path/{api_version}"
            "/functions/{lambda_arn}/invocations".format(
                region_name=region_name,
                api_version=api_version,
                lambda_arn=lambda_function_arn)
        )

    def _first_time_deploy(self, config):
        # type: (Config) -> Tuple[str, str, str]
        app_name = config.app_name
        rest_api_id = self._aws_client.create_rest_api(name=app_name)
        return self._create_resources_for_api(config, rest_api_id)

    def _create_resources_for_api(self, config, rest_api_id):
        # type: (Config, str) -> Tuple[str, str, str]
        url_trie = build_url_trie(config.chalice_app.routes)
        root_resource = self._aws_client.get_root_resource_for_api(rest_api_id)
        assert root_resource['path'] == u'/'
        resource_id = root_resource['id']
        route_builder = APIGatewayResourceCreator(
            self._aws_client,
            APIGatewayMethods(self._api_gateway_client, rest_api_id),
            config.lambda_arn)
        # This is a little confusing.  You need to specify the parent
        # resource to create a subresource, but you can't create the root
        # resource because you have to specify a parent id.  So API Gateway
        # automatically creates the root "/" resource for you. So we have
        # to query that via get_resources() and inject that into the
        # url_trie to indicate the builder shouldn't try to create the
        # resource.
        url_trie['resource_id'] = resource_id
        for child in url_trie['children']:
            url_trie['children'][child]['parent_resource_id'] = resource_id
        route_builder.build_resources(url_trie)
        # And finally, you need an actual deployment to deploy the changes to
        # API gateway.
        stage = config.stage_name or 'dev'
        print "Deploying to:", stage
        self._aws_client.deploy_rest_api(rest_api_id, stage)
        return rest_api_id, self._aws_client.region_name, stage


class OSUtils(object):
    def open(self, filename, mode):
        # type: (str, str) -> IO
        return open(filename, mode)

    def remove_file(self, filename):
        # type: (str) -> None
        """Remove a file, noop if file does not exist."""
        # Unlike os.remove, if the file does not exist,
        # then this method does nothing.
        try:
            os.remove(filename)
        except OSError:
            pass

    def file_exists(self, filename):
        # type: (str) -> bool
        return os.path.isfile(filename)

    def get_file_contents(self, filename, binary=True):
        # type: (str, bool) -> str
        if binary:
            mode = 'rb'
        else:
            mode = 'r'
        with open(filename, mode) as f:
            return f.read()


class APIGatewayMethods(object):
    """Create API gateway methods.

    This class is used to configure the various API gateway methods including:

    * Method request
    * Integration request
    * Integration response
    * Method response

    It's a higher level that the APIs provided in the API gateway
    SDK client, and provide abstractions for easily creating methods
    that support CORS, etc.

    """
    def __init__(self, apig_client, rest_api_id):
        # type: (Any, str) -> None
        self.rest_api_id = rest_api_id
        self._apig_client = apig_client

    def create_method_request(self, resource_id, http_method,
                              authorization_type=None, authorizer_id=None,
                              api_key_required=False):
        # type: (str, str, str, str, bool) -> None
        """Create an API Gateway method request.

        This defines the public API used by consumers of the API Gateway
        API.

        """
        put_method_cfg = {
            'restApiId': self.rest_api_id,
            'resourceId': resource_id,
            'httpMethod': http_method,
            'authorizationType': 'NONE'
        }  # type: Dict[str, Any]
        if authorization_type is not None:
            put_method_cfg['authorizationType'] = authorization_type
        if authorizer_id is not None:
            put_method_cfg['authorizerId'] = authorizer_id
        if api_key_required:
            put_method_cfg['apiKeyRequired'] = api_key_required
        self._apig_client.put_method(**put_method_cfg)

    def create_lambda_method_integration(self, resource_id, http_method,
                                         content_types, lambda_uri):
        # type: (str, str, List[str], str) -> None
        """Create an integration method for AWS Lambda."""
        self._apig_client.put_integration(
            restApiId=self.rest_api_id,
            resourceId=resource_id,
            type='AWS',
            httpMethod=http_method,
            integrationHttpMethod='POST',
            # Request body will never be passed through to
            # the integration, if the Content-Type header
            # is not application/json.
            passthroughBehavior="NEVER",
            requestTemplates={
                key: FULL_PASSTHROUGH for key in content_types
            },
            uri=lambda_uri,
        )

    def create_method_response(self, resource_id, http_method, cors=False):
        # type: (str, str, bool) -> None
        """Create a method response to return to API gateway consumers."""
        method_response_args = {
            'restApiId': self.rest_api_id,
            'resourceId': resource_id,
            'httpMethod': http_method,
            'statusCode': '200',
            'responseModels': {'application/json': 'Empty'},
        }
        if cors:
            method_response_args['responseParameters'] = {
                'method.response.header.Access-Control-Allow-Origin': False}
        self._apig_client.put_method_response(**method_response_args)

    def create_integration_response(self, resource_id, http_method,
                                    cors=False):
        # type: (str, str, bool) -> None
        """Create an integration response for API Gateway."""
        kwargs = {
            'restApiId': self.rest_api_id,
            'resourceId': resource_id,
            'httpMethod': http_method,
            'statusCode': '200',
            'responseTemplates': {'application/json': ''},
        }
        if cors:
            kwargs['responseParameters'] = {
                'method.response.header.Access-Control-Allow-Origin': "'*'"}
        self._apig_client.put_integration_response(**kwargs)

    def create_error_responses(self, resource_id, http_method,
                               error_classes, cors):
        # type: (str, str, List[app.ChaliceViewError], bool) -> None
        for error_cls in error_classes:
            method_response_args = {
                'restApiId': self.rest_api_id,
                'resourceId': resource_id,
                'httpMethod': http_method,
                'statusCode': str(error_cls.STATUS_CODE),
                'responseModels': {'application/json': 'Empty'},
            }
            if cors:
                method_response_args['responseParameters'] = {
                    'method.response.header.Access-Control-Allow-Origin':
                        False}
            self._apig_client.put_method_response(**method_response_args)
            integration_response_args = {
                'restApiId': self.rest_api_id,
                'resourceId': resource_id,
                'httpMethod': http_method,
                'statusCode': str(error_cls.STATUS_CODE),
                'selectionPattern': error_cls.__name__ + '.*',
                'responseTemplates': {'application/json': ERROR_MAPPING},
            }
            if cors:
                integration_response_args['responseParameters'] = {
                    'method.response.header.Access-Control-Allow-Origin':
                        "'*'"}
            self._apig_client.put_integration_response(
                **integration_response_args)

    def create_preflight_method(self, resource_id, http_methods):
        # type: (str, List[str]) -> None
        """Create preflight request for CORS.

        This will add an OPTIONS request to support preflighting
        needed by CORS.  It uses a mock integration to return
        a 200 response with the list of supported methods
        being the provided ``http_methods``.

        """
        self._apig_client.put_method(
            restApiId=self.rest_api_id,
            resourceId=resource_id,
            httpMethod='OPTIONS',
            authorizationType='NONE',
        )
        self._apig_client.put_integration(
            restApiId=self.rest_api_id,
            resourceId=resource_id,
            httpMethod='OPTIONS',
            type='MOCK',
            requestTemplates={
                'application/json': '{"statusCode": 200}',
            },
        )
        self._apig_client.put_method_response(
            restApiId=self.rest_api_id,
            resourceId=resource_id,
            httpMethod='OPTIONS',
            statusCode='200',
            responseModels={'application/json': 'Empty'},
            responseParameters={
                "method.response.header.Access-Control-Allow-Origin": False,
                "method.response.header.Access-Control-Allow-Methods": False,
                "method.response.header.Access-Control-Allow-Headers": False,
            },
        )
        if 'OPTIONS' not in http_methods:
            http_methods.append('OPTIONS')
        allowed_methods = ','.join(http_methods)
        self._apig_client.put_integration_response(
            restApiId=self.rest_api_id,
            resourceId=resource_id,
            httpMethod='OPTIONS',
            statusCode='200',
            responseTemplates={'application/json': ''},
            responseParameters={
                "method.response.header.Access-Control-Allow-Origin": "'*'",
                "method.response.header.Access-Control-Allow-Methods": (
                    "'%s'" % allowed_methods),
                "method.response.header.Access-Control-Allow-Headers": (
                    "'Content-Type,X-Amz-Date,Authorization,X-Api-Key"
                    ",X-Amz-Security-Token'")
            },
        )
