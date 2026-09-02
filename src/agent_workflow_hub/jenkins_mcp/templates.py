from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import re
import xml.etree.ElementTree as ET
from typing import Mapping


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class PluginRequirement:
    short_name: str
    minimum_version: str


@dataclass(frozen=True)
class RenderedTemplate:
    item_type: str
    template: str
    required_plugins: tuple[PluginRequirement, ...]
    expected_jenkins_class: str
    xml: str


@dataclass(frozen=True)
class TemplateIdentity:
    item_type: str
    template: str
    root_tag: str
    expected_jenkins_class: str


_TEMPLATES: dict[tuple[str, str], RenderedTemplate] = {
    (
        "folder",
        "folder-v1",
    ): RenderedTemplate(
        item_type="folder",
        template="folder-v1",
        required_plugins=(PluginRequirement("cloudbees-folder", "6.0"),),
        expected_jenkins_class="com.cloudbees.hudson.plugins.folder.Folder",
        xml="""<com.cloudbees.hudson.plugins.folder.Folder plugin="cloudbees-folder"><actions/><description/><properties/><folderViews class="com.cloudbees.hudson.plugins.folder.views.DefaultFolderViewHolder"><views><hudson.model.AllView><owner class="com.cloudbees.hudson.plugins.folder.Folder" reference="../../../.."/><name>All</name><filterExecutors>false</filterExecutors><filterQueue>false</filterQueue><properties class="hudson.model.View$PropertyList"/></hudson.model.AllView></views><tabBar class="hudson.views.DefaultViewsTabBar"/></folderViews><healthMetrics/><icon class="com.cloudbees.hudson.plugins.folder.icons.StockFolderIcon"/></com.cloudbees.hudson.plugins.folder.Folder>""",
    ),
    (
        "freestyle",
        "freestyle-v1",
    ): RenderedTemplate(
        item_type="freestyle",
        template="freestyle-v1",
        required_plugins=(),
        expected_jenkins_class="hudson.model.FreeStyleProject",
        xml="""<project><actions/><description>Configured by Jenkins MCP</description><keepDependencies>false</keepDependencies><properties/><scm class="hudson.scm.NullSCM"/><canRoam>true</canRoam><disabled>false</disabled><blockBuildWhenDownstreamBuilding>false</blockBuildWhenDownstreamBuilding><blockBuildWhenUpstreamBuilding>false</blockBuildWhenUpstreamBuilding><triggers/><concurrentBuild>false</concurrentBuild><builders/><publishers/><buildWrappers/></project>""",
    ),
    (
        "pipeline",
        "pipeline-v1",
    ): RenderedTemplate(
        item_type="pipeline",
        template="pipeline-v1",
        required_plugins=(
            PluginRequirement("workflow-job", "2.0"),
            PluginRequirement("workflow-cps", "2.0"),
        ),
        expected_jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        xml="""<flow-definition plugin="workflow-job"><actions/><description>Configured by Jenkins MCP</description><keepDependencies>false</keepDependencies><properties/><definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps"><script>echo 'Configured by Jenkins MCP'</script><sandbox>true</sandbox></definition><triggers/><disabled>false</disabled></flow-definition>""",
    ),
    (
        "view",
        "view-v1",
    ): RenderedTemplate(
        item_type="view",
        template="view-v1",
        required_plugins=(),
        expected_jenkins_class="hudson.model.ListView",
        xml="""<hudson.model.ListView><filterExecutors>false</filterExecutors><filterQueue>false</filterQueue><properties class="hudson.model.View$PropertyList"/><jobNames class="tree-set"/><jobFilters/><columns><hudson.views.StatusColumn/><hudson.views.WeatherColumn/><hudson.views.JobColumn/><hudson.views.LastSuccessColumn/><hudson.views.LastFailureColumn/><hudson.views.LastDurationColumn/><hudson.views.BuildButtonColumn/></columns><recurse>false</recurse></hudson.model.ListView>""",
    ),
    (
        "multibranch",
        "multibranch-v1",
    ): RenderedTemplate(
        item_type="multibranch",
        template="multibranch-v1",
        required_plugins=(PluginRequirement("workflow-multibranch", "2.0"),),
        expected_jenkins_class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject",
        xml="""<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject plugin="workflow-multibranch"><actions/><description>Configured by Jenkins MCP</description><properties/><folderViews class="jenkins.branch.MultiBranchProjectViewHolder"/><healthMetrics/><icon class="jenkins.branch.MetadataActionFolderIcon"/><orphanedItemStrategy class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy"><pruneDeadBranches>false</pruneDeadBranches><daysToKeep>-1</daysToKeep><numToKeep>-1</numToKeep><abortBuilds>false</abortBuilds></orphanedItemStrategy><triggers/><sources class="jenkins.branch.MultiBranchProject$BranchSourceList"/><factory class="org.jenkinsci.plugins.workflow.multibranch.WorkflowBranchProjectFactory"><owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/><scriptPath>Jenkinsfile</scriptPath></factory><disabled>false</disabled></org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>""",
    ),
}

_PYTEST_TEMPLATE = "pytest-inline-v1"
_PYTEST_PARAMETERS = frozenset(
    {
        "repository_url",
        "credentials_id",
        "agent_label",
        "commit_sha",
        "branch",
        "requirements_path",
        "test_path",
        "runner_os",
        "pytest_args",
    }
)
_GROOVY_TEMPLATE = "groovy-inline-v1"
_GROOVY_PARAMETERS = frozenset({"script"})
_JENKINSFILE_SCM_TEMPLATE = "jenkinsfile-scm-v1"
_JENKINSFILE_SCM_PARAMETERS = frozenset(
    {
        "repository_url",
        "branch",
        "script_path",
        "credentials_id",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_BRANCH = re.compile(r"^test/[A-Za-z0-9][A-Za-z0-9._/-]{0,126}$")
_SAFE_BRANCH_SPEC = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._/-]{0,255}$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_PYTEST_FIXED_ARGS = frozenset({"-q", "-x", "--disable-warnings"})
_PYTEST_VALUE_ARGS = re.compile(r"^--(?:maxfail=[1-9][0-9]*|tb=(?:short|long|line|no))$")


def template_identity(*, item_type: str, template: str) -> TemplateIdentity:
    if item_type == "pipeline" and template in {
        "pipeline-v1",
        _PYTEST_TEMPLATE,
        _GROOVY_TEMPLATE,
        _JENKINSFILE_SCM_TEMPLATE,
    }:
        return TemplateIdentity(
            item_type=item_type,
            template=template,
            root_tag="flow-definition",
            expected_jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        )
    rendered = _TEMPLATES.get((item_type, template))
    if rendered is None:
        raise TemplateError(f"unsupported Jenkins item template {item_type!r}/{template!r}")
    return TemplateIdentity(
        item_type=item_type,
        template=template,
        root_tag=ET.fromstring(rendered.xml).tag,
        expected_jenkins_class=rendered.expected_jenkins_class,
    )


def render_template(
    *,
    item_type: str,
    template: str,
    parameters: Mapping[str, str],
) -> RenderedTemplate:
    if item_type == "pipeline" and template == _PYTEST_TEMPLATE:
        return _render_pytest_inline_v1(parameters)
    if item_type == "pipeline" and template == _GROOVY_TEMPLATE:
        return _render_groovy_inline_v1(parameters)
    if item_type == "pipeline" and template == _JENKINSFILE_SCM_TEMPLATE:
        return _render_jenkinsfile_scm_v1(parameters)
    rendered = _TEMPLATES.get((item_type, template))
    if rendered is None:
        raise TemplateError(f"unsupported Jenkins item template {item_type!r}/{template!r}")
    if parameters:
        raise TemplateError(f"template {template!r} does not accept parameters")
    return rendered


def _render_pytest_inline_v1(parameters: Mapping[str, str]) -> RenderedTemplate:
    values = _validate_pytest_parameters(parameters)
    script = _pytest_pipeline_script(values)
    return RenderedTemplate(
        item_type="pipeline",
        template=_PYTEST_TEMPLATE,
        required_plugins=(
            PluginRequirement("workflow-job", "2.0"),
            PluginRequirement("workflow-cps", "2.0"),
            PluginRequirement("pipeline-model-definition", "1.0"),
            PluginRequirement("workflow-scm-step", "1.0"),
            PluginRequirement("workflow-durable-task-step", "1.0"),
            PluginRequirement("git", "1.0"),
            PluginRequirement("junit", "1.0"),
        ),
        expected_jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        xml=(
            '<flow-definition plugin="workflow-job"><actions/>'
            "<description>Configured pytest execution by Jenkins MCP</description>"
            "<keepDependencies>false</keepDependencies><properties/>"
            '<definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" '
            'plugin="workflow-cps"><script>'
            + escape(script, quote=False)
            + "</script><sandbox>true</sandbox></definition><triggers/><disabled>false</disabled>"
            "</flow-definition>"
        ),
    )


def _render_groovy_inline_v1(parameters: Mapping[str, str]) -> RenderedTemplate:
    values = _validate_groovy_parameters(parameters)
    script = values["script"]
    return RenderedTemplate(
        item_type="pipeline",
        template=_GROOVY_TEMPLATE,
        required_plugins=(
            PluginRequirement("workflow-job", "2.0"),
            PluginRequirement("workflow-cps", "2.0"),
        ),
        expected_jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        xml=(
            '<flow-definition plugin="workflow-job"><actions/>'
            "<description>Configured inline Groovy pipeline by Jenkins MCP</description>"
            "<keepDependencies>false</keepDependencies><properties/>"
            '<definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" '
            'plugin="workflow-cps"><script>'
            + escape(script, quote=False)
            + "</script><sandbox>true</sandbox></definition><triggers/><disabled>false</disabled>"
            "</flow-definition>"
        ),
    )


def _render_jenkinsfile_scm_v1(parameters: Mapping[str, str]) -> RenderedTemplate:
    values = _validate_jenkinsfile_scm_parameters(parameters)
    repository_url = values["repository_url"]
    branch = values["branch"]
    script_path = values["script_path"]
    credentials_id = values["credentials_id"]
    user_remote_config = (
        "<hudson.plugins.git.UserRemoteConfig><url>"
        + escape(repository_url, quote=False)
        + "</url>"
        + (
            f"<credentialsId>{credentials_id}</credentialsId>"
            if credentials_id
            else ""
        )
        + "</hudson.plugins.git.UserRemoteConfig>"
    )
    return RenderedTemplate(
        item_type="pipeline",
        template=_JENKINSFILE_SCM_TEMPLATE,
        required_plugins=(
            PluginRequirement("workflow-job", "2.0"),
            PluginRequirement("workflow-cps", "2.0"),
            PluginRequirement("git", "1.0"),
        ),
        expected_jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        xml=(
            '<flow-definition plugin="workflow-job"><actions/>'
            "<description>Configured SCM Jenkinsfile pipeline by Jenkins MCP</description>"
            "<keepDependencies>false</keepDependencies><properties/>"
            '<definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" '
            'plugin="workflow-cps"><scm class="hudson.plugins.git.GitSCM" plugin="git">'
            "<configVersion>2</configVersion><userRemoteConfigs>"
            + user_remote_config
            + "</userRemoteConfigs><branches><hudson.plugins.git.BranchSpec><name>"
            + branch
            + "</name></hudson.plugins.git.BranchSpec></branches>"
            "<doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>"
            '<submoduleCfg class="empty-list"/><extensions/></scm>'
            "<scriptPath>"
            + script_path
            + "</scriptPath><lightweight>true</lightweight></definition>"
            "<triggers/><disabled>false</disabled></flow-definition>"
        ),
    )


def _validate_pytest_parameters(parameters: Mapping[str, str]) -> dict[str, object]:
    if not isinstance(parameters, Mapping):
        raise TemplateError("pytest-inline-v1 parameters must be a mapping")
    keys = set(parameters)
    missing = _PYTEST_PARAMETERS - keys
    unknown = keys - _PYTEST_PARAMETERS
    if missing:
        raise TemplateError(f"pytest-inline-v1 parameters are missing {sorted(missing)}")
    if unknown:
        raise TemplateError(f"pytest-inline-v1 parameters are unknown {sorted(unknown)}")
    if not all(isinstance(value, str) for value in parameters.values()):
        raise TemplateError("pytest-inline-v1 parameter values must be strings")

    repository_url = _validate_repository_url(parameters["repository_url"])
    credentials_id = _validate_identifier(parameters["credentials_id"], "credentials ID")
    agent_label = _validate_identifier(parameters["agent_label"], "agent label")
    commit_sha = parameters["commit_sha"]
    if _COMMIT_SHA.fullmatch(commit_sha) is None:
        raise TemplateError("commit SHA must be a lowercase full SHA")
    branch = parameters["branch"]
    if (
        _SAFE_BRANCH.fullmatch(branch) is None
        or ".." in branch
        or "//" in branch
        or branch.endswith("/")
    ):
        raise TemplateError("temporary branch is invalid")
    requirements_path = validate_repository_path(
        parameters["requirements_path"], "requirements path"
    )
    test_path = validate_repository_path(parameters["test_path"], "test path")
    runner_os = parameters["runner_os"]
    if runner_os not in {"linux", "windows"}:
        raise TemplateError("runner OS is unsupported")
    pytest_args = _parse_pytest_args(parameters["pytest_args"])
    return {
        "repository_url": repository_url,
        "credentials_id": credentials_id,
        "agent_label": agent_label,
        "commit_sha": commit_sha,
        "branch": branch,
        "requirements_path": requirements_path,
        "test_path": test_path,
        "runner_os": runner_os,
        "pytest_args": pytest_args,
    }


def _validate_groovy_parameters(parameters: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(parameters, Mapping):
        raise TemplateError("groovy-inline-v1 parameters must be a mapping")
    keys = set(parameters)
    missing = _GROOVY_PARAMETERS - keys
    unknown = keys - _GROOVY_PARAMETERS
    if missing:
        raise TemplateError(f"groovy-inline-v1 parameters are missing {sorted(missing)}")
    if unknown:
        raise TemplateError(f"groovy-inline-v1 parameters are unknown {sorted(unknown)}")
    if not all(isinstance(value, str) for value in parameters.values()):
        raise TemplateError("groovy-inline-v1 parameter values must be strings")
    script = parameters["script"]
    if not script.strip():
        raise TemplateError("Groovy script must be a nonblank string")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in script):
        raise TemplateError("Groovy script must not contain XML control characters")
    return {"script": script}


def _validate_jenkinsfile_scm_parameters(parameters: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(parameters, Mapping):
        raise TemplateError("jenkinsfile-scm-v1 parameters must be a mapping")
    keys = set(parameters)
    missing = _JENKINSFILE_SCM_PARAMETERS - keys
    unknown = keys - _JENKINSFILE_SCM_PARAMETERS
    if missing:
        raise TemplateError(f"jenkinsfile-scm-v1 parameters are missing {sorted(missing)}")
    if unknown:
        raise TemplateError(f"jenkinsfile-scm-v1 parameters are unknown {sorted(unknown)}")
    if not all(isinstance(value, str) for value in parameters.values()):
        raise TemplateError("jenkinsfile-scm-v1 parameter values must be strings")
    repository_url = _validate_repository_url(parameters["repository_url"])
    branch = parameters["branch"]
    if (
        _SAFE_BRANCH_SPEC.fullmatch(branch) is None
        or ".." in branch.split("/")
        or "//" in branch
        or branch.startswith("/")
        or branch.endswith("/")
    ):
        raise TemplateError("branch or refspec is invalid")
    script_path = parameters["script_path"]
    if script_path:
        script_path = validate_repository_path(script_path, "script path")
    else:
        script_path = "Jenkinsfile"
    credentials_id = parameters["credentials_id"]
    if credentials_id:
        credentials_id = _validate_identifier(credentials_id, "credentials ID")
    return {
        "repository_url": repository_url,
        "branch": branch,
        "script_path": script_path,
        "credentials_id": credentials_id,
    }


def _validate_repository_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(
        ord(character) < 32 and character not in "\t\n\r" for character in value
    ):
        raise TemplateError("repository URL is invalid")
    return value


def _validate_identifier(value: str, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise TemplateError(f"{label} is invalid")
    return value


def validate_repository_path(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_PATH.fullmatch(value) is None
        or ".." in value.split("/")
        or "//" in value
        or value.endswith("/")
    ):
        raise TemplateError(f"{label} is invalid")
    return value


def _parse_pytest_args(value: str) -> tuple[str, ...]:
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise TemplateError("pytest arguments must be a JSON array") from None
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item for item in raw
    ):
        raise TemplateError("pytest arguments must be a JSON string array")
    return validate_pytest_args(tuple(raw))


def validate_pytest_args(arguments: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(arguments, tuple) or not all(
        isinstance(argument, str) and argument for argument in arguments
    ):
        raise TemplateError("pytest args must be a tuple of non-empty strings")
    for argument in arguments:
        if argument in _PYTEST_FIXED_ARGS or _PYTEST_VALUE_ARGS.fullmatch(argument):
            continue
        raise TemplateError("pytest args contain an unsupported argument")
    return arguments


def _groovy_single_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _pytest_pipeline_script(values: Mapping[str, object]) -> str:
    repository_url = values["repository_url"]
    credentials_id = values["credentials_id"]
    agent_label = values["agent_label"]
    commit_sha = values["commit_sha"]
    branch = values["branch"]
    requirements_path = values["requirements_path"]
    test_path = values["test_path"]
    runner_os = values["runner_os"]
    pytest_args = values["pytest_args"]
    assert all(
        isinstance(value, str)
        for value in (
            repository_url,
            credentials_id,
            agent_label,
            commit_sha,
            branch,
            requirements_path,
            test_path,
            runner_os,
        )
    )
    assert isinstance(pytest_args, tuple) and all(
        isinstance(value, str) for value in pytest_args
    )
    runner_step = "bat" if runner_os == "windows" else "sh"
    pytest_command = " ".join(
        (
            "python -m pytest",
            *pytest_args,
            test_path,
            "--junitxml=reports/junit.xml",
        )
    )
    return "\n".join(
        (
            "pipeline {",
            f"  agent {{ label '{agent_label}' }}",
            "  options { skipDefaultCheckout(true) }",
            "  stages {",
            "    stage('Checkout exact commit') {",
            "      steps {",
            "        checkout([$class: 'GitSCM', branches: [[name: '*/"
            + branch
            + "']], userRemoteConfigs: [[url: '"
            + _groovy_single_quote(repository_url)
            + "', credentialsId: '"
            + credentials_id
            + "']]])",
            f"        {runner_step} 'git fetch --no-tags origin refs/heads/{branch}:refs/remotes/origin/{branch}'",
            f"        {runner_step} 'git merge-base --is-ancestor {commit_sha} origin/{branch}'",
            f"        {runner_step} 'git checkout --detach {commit_sha}'",
            f"        {runner_step} 'python -m pip install --requirement {requirements_path}'",
            f"        {runner_step} '{pytest_command}'",
            "      }",
            "    }",
            "  }",
            "  post {",
            "    always { junit allowEmptyResults: false, testResults: 'reports/junit.xml' }",
            "  }",
            "}",
        )
    )
