import os
import json
import glob
import logging
import sqlite3
import datetime
from urllib.parse import urlparse, unquote
from memoization import cached
from typing import List

LOGGING = logging.getLogger(__name__)


class Client(object):
    """ Client to interact with VS Code: API"""

    @cached(ttl=60)
    def get_projects(self, preferences: dict):
        """ Returns projects """

        project_manager_projects = []
        workspace_projects = []

        names_index = []
        if preferences.get('include_project_manager', False):
            project_manager_projects = self.get_projects_from_project_manager(
                preferences)

            names_index = [p['name'] for p in project_manager_projects]

        if preferences.get('include_recent_workspaces', True):
            if preferences.get('use_shared_state_db', False):
                workspace_projects = self.get_projects_from_shared_state_db(
                    preferences, names_index)
            else:
                workspace_projects = self.get_projects_from_workspaces(
                    preferences, names_index)

        all_projects = project_manager_projects + workspace_projects
        return all_projects

    def get_projects_from_project_manager(self, preferences):
        """ Reads projects saved by the Project Manager VS Code: extension """
        mapped_projects = []
        projects = []
        full_project_path = os.path.expanduser(
            preferences['projects_file_path'])

        try:
            if os.path.isfile(full_project_path):
                with open(full_project_path) as projects_file:
                    projects = json.load(projects_file)
            elif os.path.isdir(full_project_path):
                project_files = os.listdir(full_project_path)

                for project_file_name in project_files:
                    project_file_path = os.path.join(
                        full_project_path, project_file_name)
                    try:
                        with open(project_file_path) as file:
                            projects += json.load(file)
                    except (OSError, json.JSONDecodeError):
                        LOGGING.exception(
                            'Failed to read Project Manager file: %s',
                            project_file_path)
            else:
                LOGGING.warning(
                    'Project Manager path does not exist: %s',
                    full_project_path)
        except (OSError, json.JSONDecodeError):
            LOGGING.exception(
                'Failed to read Project Manager projects from: %s',
                full_project_path)
            return mapped_projects

        for project in projects:
            name = project.get('name')
            path = project.get('fullPath') or project.get('rootPath')

            if not name or not path:
                LOGGING.warning(
                    'Skipping malformed Project Manager entry: %s', project)
                continue

            mapped_projects.append({
                'name': name,
                'path': path,
                'type': 'project',
            })

        return mapped_projects

    def get_projects_from_workspaces(self, preferences: dict,
                                     exclude_list: List[str]):
        """ Reads VS Code: recent workspaces from the per-window
        workspaceStorage folder """
        abs_path = os.path.expanduser(preferences['config_path'])
        file_list = glob.glob(abs_path +
                              "/User/workspaceStorage/*/workspace.json")

        recent_workspaces = []
        for workspace_file in file_list:
            try:
                with open(workspace_file, 'r') as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                LOGGING.exception(
                    'Failed to read workspace file: %s', workspace_file)
                continue

            if 'folder' not in data:
                continue

            folder_uri = data['folder']
            if not folder_uri.startswith('file://'):
                # Skip remote (vscode-remote://) or otherwise non-local
                # folders; this data source can't verify they still exist.
                continue

            path = unquote(folder_uri[len('file://'):])
            # get workspace name
            name_pointer = path.rstrip('/').rfind('/')
            name = path.rstrip('/')[name_pointer + 1:]

            # make sure to include only folders that still exist
            if not os.path.isdir(path) or path.rstrip('/') == '/tmp':
                continue

            # use the workspace-storage entry's own mtime (updated whenever
            # VS Code: opens/saves state for that workspace) rather than the
            # target folder's mtime, which can change for reasons unrelated
            # to VS Code: (e.g. git pull, another editor, a background job)
            mtime = datetime.datetime.fromtimestamp(
                os.stat(workspace_file).st_mtime)
            recent_workspaces.append({'name': name, 'path': path, 'mtime': mtime})

        recent_workspaces.sort(reverse=True, key=lambda p: p['mtime'])
        projects = []
        for w in recent_workspaces:
            if w['name'] in exclude_list:
                continue

            projects.append({
                'name': w['name'],
                'path': w['path'],
                'type': 'workspace'
            })

        return projects

    def get_projects_from_shared_state_db(self, preferences: dict,
                                          exclude_list: List[str]):
        """ Reads VS Code: recent workspaces (and optionally recent files)
        from the shared "Recent Workspaces" state database
        (<shared_storage_path>/sharedStorage/state.vscdb) """
        shared_base_path = os.path.expanduser(
            preferences.get('shared_storage_path', '~/.vscode-shared/'))
        db_path = os.path.join(shared_base_path, 'sharedStorage', 'state.vscdb')

        if not os.path.isfile(db_path):
            LOGGING.warning('Shared state database not found: %s', db_path)
            return []

        include_files = preferences.get('include_files', False)

        raw_value = self._read_recent_paths_from_db(db_path)
        if raw_value is None:
            return []

        try:
            data = json.loads(raw_value)
        except json.JSONDecodeError:
            LOGGING.exception(
                'Malformed JSON in shared state database: %s', db_path)
            return []

        projects = []
        for entry in data.get('entries', []):
            entry_uri, entry_type = self._entry_uri_and_type(
                entry, include_files)

            if not entry_uri:
                continue

            path = self._uri_to_path(entry_uri)
            name = os.path.basename(path.rstrip('/')) or path

            if name in exclude_list:
                continue

            # Only local (file://) entries can be verified to still exist;
            # remote entries (e.g. vscode-remote://) are kept as-is, matching
            # how ExtensionCustomAction/OpenAction already handle them.
            if urlparse(entry_uri).scheme == 'file':
                if path.rstrip('/') == '/tmp':
                    continue
                exists = (os.path.isfile(path) if entry_type == 'file'
                          else os.path.isdir(path))
                if not exists:
                    continue

            projects.append({'name': name, 'path': path, 'type': entry_type})

        return projects

    @staticmethod
    def _read_recent_paths_from_db(db_path):
        """ Opens the sqlite database read-only (so we never write to or
        lock a database VS Code: itself might have open) and returns the raw
        value of the 'history.recentlyOpenedPathsList' key, or None """
        try:
            connection = sqlite3.connect(
                'file:{}?mode=ro'.format(db_path), uri=True)
        except sqlite3.Error:
            LOGGING.exception(
                'Failed to open shared state database: %s', db_path)
            return None

        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                ('history.recentlyOpenedPathsList',))
            row = cursor.fetchone()
        except sqlite3.Error:
            LOGGING.exception(
                'Failed to query shared state database: %s', db_path)
            return None
        finally:
            connection.close()

        if not row or not row[0]:
            return None

        value = row[0]
        return value.decode('utf-8') if isinstance(value, bytes) else value

    @staticmethod
    def _entry_uri_and_type(entry, include_files):
        """ Extracts (uri, type) from a recentlyOpenedPathsList entry, or
        (None, None) if the entry should be skipped """
        if 'folderUri' in entry:
            return entry['folderUri'], 'workspace'

        if isinstance(entry.get('workspace'), dict):
            config_path = entry['workspace'].get('configPath')
            if config_path:
                return config_path, 'workspace'
            return None, None

        if 'fileUri' in entry:
            if not include_files:
                return None, None
            return entry['fileUri'], 'file'

        return None, None

    @staticmethod
    def _uri_to_path(uri_str):
        """ Converts a file:// URI to a local filesystem path (decoding
        percent-escapes like %20). Non-local URIs (e.g. vscode-remote://)
        are returned unchanged. """
        parsed = urlparse(uri_str)
        if parsed.scheme == 'file':
            return unquote(parsed.path)
        return uri_str
