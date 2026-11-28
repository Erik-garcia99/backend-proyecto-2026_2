"""Contrato del borrador con BD falsa; no sustituye pruebas SQL en PostgreSQL."""
import copy
import unittest
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.store_maps import MapDocument, create_router

ORG = UUID('11111111-1111-4111-8111-111111111111')
BRANCH = UUID('22222222-2222-4222-8222-222222222222')
OTHER = UUID('33333333-3333-4333-8333-333333333333')
USER = UUID('44444444-4444-4444-8444-444444444444')
ELEMENT = '55555555-5555-4555-8555-555555555555'
PATH = f'/branches/{BRANCH}/map/draft'


def document(**changes):
    return {'schema_version': 1, 'width': 100, 'height': 100, 'elements': [], **changes}


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return copy.deepcopy(self.row)


class Database:
    def __init__(self):
        self.row = None
        self.branch_active = True
        self.queries = []
        self.logs = []
        self.commits = 0
        self.opens = 0

    def connect(self):
        self.opens += 1
        return self

    def __enter__(self):
        self.snapshot = copy.deepcopy((self.row, self.logs))
        return self

    def __exit__(self, typ, value, traceback):
        if typ:
            self.row, self.logs = self.snapshot

    def commit(self):
        self.commits += 1

    def execute(self, sql, args=()):
        query = ' '.join(sql.lower().split())
        self.queries.append(query)
        if 'from branch b' in query:
            return Result({'id': BRANCH, 'organization_id': ORG}) if self.branch_active else Result()
        if query.startswith('insert into store_map'):
            if self.row is None:
                self.row = {'draft_document': None, 'draft_version': 0,
                            'draft_updated_by': None, 'draft_updated_at': None}
            return Result()
        if query.startswith('select draft_document'):
            return Result(self.row)
        if query.startswith('update store_map'):
            self.row.update(draft_document=args[0].obj,
                            draft_version=self.row['draft_version'] + 1,
                            draft_updated_by=args[1],
                            draft_updated_at=datetime.now(timezone.utc))
            return Result(self.row)
        if query.startswith('insert into system_log'):
            self.logs.append(args)
            return Result()
        raise AssertionError(f'Consulta inesperada: {query}')


class MapDraftTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.user = {'id': USER, 'organization_id': ORG, 'branch_id': BRANCH,
                     'role': 'manager', 'is_superuser': False}
        app = FastAPI()
        app.include_router(create_router(lambda: self.user, self.db.connect))
        self.client = TestClient(app)

    def save(self, base=0, value=None):
        return self.client.put(PATH, json={'base_version': base, 'document': value or document()})

    def test_no_map_returns_empty_revision_without_creating_row(self):
        response = self.client.get(PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['version'], 0)
        self.assertIsNone(response.json()['document'])
        self.assertIsNone(self.db.row)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.db.commits, 0)

    def test_save_then_reopen_and_audit_without_touching_legacy_or_hardware(self):
        response = self.save()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['version'], 1)
        self.assertEqual(response.json()['document'], document())
        self.assertEqual(self.client.get(PATH).json(), response.json())
        self.assertEqual(self.db.logs[0][3], 'map.draft_saved')
        self.assertEqual(self.db.commits, 1)
        self.assertTrue(any('for update' in sql for sql in self.db.queries))
        self.assertFalse(any('width_cm=' in sql or 'update shelf' in sql
                             or 'esp32' in sql for sql in self.db.queries))

    def test_editor_roles_and_superuser(self):
        for role in ('owner', 'manager', 'gerencia', 'sudo'):
            with self.subTest(role=role):
                self.user['role'] = role
                self.assertEqual(self.client.get(PATH).status_code, 200)
                current = self.db.row['draft_version'] if self.db.row else 0
                self.assertEqual(self.save(current).status_code, 200)
        self.user.update(role='employee', is_superuser=True, organization_id=OTHER, branch_id=None)
        self.assertEqual(self.client.get(PATH).status_code, 200)

    def test_rh_staff_customer_and_unknown_cannot_read_or_write_private_draft(self):
        for role in ('rh', 'employee', 'staff', 'customer', 'unknown'):
            with self.subTest(role=role):
                self.user['role'] = role
                self.assertEqual(self.client.get(PATH).status_code, 403)
                self.assertEqual(self.save().status_code, 403)
        self.assertEqual(self.db.opens, 0)

    def test_cross_branch_or_organization_forbidden(self):
        for updates in ({'branch_id': OTHER}, {'organization_id': OTHER},
                        {'role': 'owner', 'organization_id': OTHER}):
            with self.subTest(updates=updates):
                original = self.user.copy()
                self.user.update(updates)
                self.assertEqual(self.client.get(PATH).status_code, 403)
                self.assertEqual(self.save().status_code, 403)
                self.user = original
        self.assertIsNone(self.db.row)

    def test_owner_and_gerencia_keep_organization_wide_access(self):
        for role in ('owner', 'gerencia'):
            self.user.update(role=role, branch_id=OTHER)
            self.assertEqual(self.client.get(PATH).status_code, 200)

    def test_inactive_branch_returns_404(self):
        self.db.branch_active = False
        self.assertEqual(self.client.get(PATH).status_code, 404)
        self.assertEqual(self.save().status_code, 404)
        self.assertIsNone(self.db.row)

    def test_two_sessions_cannot_overwrite_and_identical_current_save_is_noop(self):
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.save(0, document(width=120)).status_code, 409)
        self.assertEqual(self.db.row['draft_document']['width'], 100)
        self.assertEqual(self.save(1).json()['version'], 1)
        self.assertEqual(len(self.db.logs), 1)
        self.assertEqual(self.save(1, document(width=120)).json()['version'], 2)
        self.assertEqual(len(self.db.logs), 2)

    def test_invalid_requests_have_no_writes(self):
        element = {'id': ELEMENT, 'kind': 'rectangle', 'x': 0, 'y': 0,
                   'width': 10, 'height': 10}
        cases = [
            {'base_version': -1, 'document': document()},
            {'base_version': True, 'document': document()},
            {'base_version': 0, 'document': document(width='100')},
            {'base_version': 0, 'document': document(width=True)},
            {'base_version': 0, 'document': document(height=0)},
            {'base_version': 0, 'document': document(width=10001)},
            {'base_version': 0, 'document': document(elements=[element, element])},
            {'base_version': 0, 'document': document(elements=[{**element, 'x': 95}])},
            {'base_version': 0, 'document': document(elements=[{**element, 'rotation': 45}])},
            {'base_version': 0, 'document': document(elements=[{**element, 'shelf_id': str(OTHER)}])},
            {'base_version': 0, 'document': document(elements=[{**element, 'label': 'x' * 81}])},
            {'base_version': 0, 'document': document(elements=[element] * 201)},
        ]
        for body in cases:
            with self.subTest(body=str(body)[:150]):
                self.assertEqual(self.client.put(PATH, json=body).status_code, 422)
        self.assertEqual(self.db.opens, 0)

    def test_valid_rotated_geometry_and_all_kinds(self):
        for kind in ('rectangle', 'ellipse', 'hexagon', 'wall', 'door', 'double_door', 'window'):
            value = document(elements=[{'id': ELEMENT, 'kind': kind, 'x': 25,
                                       'y': 25, 'width': 10, 'height': 20, 'rotation': 45}])
            base = self.db.row['draft_version'] if self.db.row else 0
            self.assertEqual(self.save(base, value).status_code, 200)
        for value in (float('inf'), float('nan'), float('-inf')):
            with self.assertRaises(ValidationError):
                MapDocument.model_validate(document(width=value))

    def test_audit_failure_rolls_back_the_draft(self):
        from unittest.mock import patch
        with patch('app.store_maps.record_change', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.save()
        self.assertIsNone(self.db.row)
        self.assertEqual(self.db.commits, 0)

    def test_router_registered_in_main(self):
        from app.main import app
        paths = app.openapi()['paths']
        self.assertIn('/branches/{branch_id}/map/draft', paths)
        self.assertEqual(set(paths['/branches/{branch_id}/map/draft']), {'get', 'put'})
