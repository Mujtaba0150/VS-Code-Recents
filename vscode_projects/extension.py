""" Main Module """

import logging
import os
import subprocess
from ulauncher.api import Extension, Result
from ulauncher.api.shared.action.ExtensionCustomAction import ExtensionCustomAction
from ulauncher.api.shared.action.OpenAction import OpenAction
from ulauncher.api.shared.action.HideWindowAction import HideWindowAction

from vscode_projects.vscode import Client

LOGGING = logging.getLogger(__name__)

MAX_PROJECTS_IN_LIST = 8


class VSCodeProjectsExtension(Extension):
    """ Main Extension Class  """

    def __init__(self):
        """ Initializes the extension """
        super().__init__()
        self.vscode = Client()

    def on_input(self, input_text: str, trigger_id: str):
        """ Handle user input and return matching projects """
        query = input_text or ""
        projects = self.vscode.get_projects(self.preferences)

        if query:
            projects = [
                item for item in projects
                if query.strip().lower() in item['name'].lower()
            ]

        if not projects:
            yield Result(
                icon='images/icon.png',
                name='No projects found matching your query: %s' % query,
                highlightable=False,
                on_enter=HideWindowAction()
            )
            return

        for project in projects[:MAX_PROJECTS_IN_LIST]:
            icon = 'images/icon.png'

            if project['type'] in ('workspace', 'file'):
                icon = 'images/code-dark-icon.png'

            yield Result(
                icon=icon,
                name=project['name'],
                description=project['path'],
                on_enter=ExtensionCustomAction({
                    'path': project['path'],
                    'type': project['type'],
                }),
                on_alt_enter=OpenAction(project['path'])
            )

    def on_item_enter(self, data):
        """ Handle the click on an item of the extension """
        code_executable = self.preferences['code_executable_path']
        new_env = os.environ.copy()
        new_env.pop('PYTHONPATH', None)

        path = data['path']
        is_remote = path.startswith('vscode-remote://')

        try:
            if not is_remote:
                subprocess.run([code_executable, path], env=new_env)
            elif data.get('type') == 'file':
                subprocess.run(
                    [code_executable, '--file-uri', path], env=new_env)
            else:
                subprocess.run(
                    [code_executable, '--folder-uri', path], env=new_env)
        except OSError:
            LOGGING.exception(
                'Failed to launch VS Code: executable: %s', code_executable)

        return HideWindowAction()
