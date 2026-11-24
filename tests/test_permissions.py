import unittest

from fastapi import HTTPException

from app.permissions import (
    ADMIN, CUSTOMER, STAFF, can_approve_users, can_create_users, is_admin,
    is_customer, is_staff, is_superuser, require_actor, require_branch_actor,
)

BRANCH = 'b1a7c0de-3f25-4c8a-9b6e-7d2f5a9c1e33'
OTHER_BRANCH = 'c1a7c0de-3f25-4c8a-9b6e-7d2f5a9c1e33'
ORG = 'org-1'


def user(role, *, branch_id=BRANCH, organization_id=ORG, superuser=False):
    return {'role': role, 'is_superuser': superuser,
            'branch_id': branch_id, 'organization_id': organization_id}


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, organization_id=ORG):
        self.organization_id = organization_id

    def execute(self, _sql, params):
        return FakeCursor({'id': params[0], 'organization_id': self.organization_id})


class RoleTests(unittest.TestCase):
    def test_create_and_approve_are_separate_powers(self):
        for role in ('owner', 'rh', 'manager', 'gerencia', 'sudo'):
            with self.subTest(creator=role):
                self.assertTrue(can_create_users(user(role, superuser=role == 'sudo')))
        for role in ('employee', 'staff', 'customer'):
            with self.subTest(creator=role):
                self.assertFalse(can_create_users(user(role)))

        for role in ('owner', 'rh', 'sudo'):
            with self.subTest(approver=role):
                self.assertTrue(can_approve_users(user(role, superuser=role == 'sudo')))
        for role in ('manager', 'gerencia', 'employee', 'staff', 'customer'):
            with self.subTest(approver=role):
                self.assertFalse(can_approve_users(user(role)))

    def test_predicates(self):
        self.assertTrue(is_superuser(user('sudo', superuser=True)))
        self.assertTrue(is_admin(user('owner')))
        self.assertTrue(is_admin(user('gerencia')))
        self.assertTrue(is_admin(user('sudo', superuser=True)))
        self.assertFalse(is_admin(user('employee')))
        self.assertTrue(is_staff(user('employee')))
        self.assertTrue(is_staff(user('staff')))
        self.assertTrue(is_customer(user('customer')))
        self.assertFalse(is_customer(user('employee')))

    def test_require_actor_admin(self):
        for role in ('owner', 'manager', 'rh', 'gerencia', 'sudo'):
            with self.subTest(role=role):
                require_actor(user(role, superuser=role == 'sudo'), ADMIN)
        for role in ('employee', 'staff', 'customer'):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as failure:
                    require_actor(user(role), ADMIN)
                self.assertEqual(failure.exception.status_code, 403)

    def test_require_actor_staff_and_customer(self):
        for role in ('employee', 'staff', 'sudo'):
            require_actor(user(role, superuser=role == 'sudo'), STAFF)
        for role in ('employee', 'staff', 'manager'):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException):
                    require_actor(user(role), CUSTOMER)
        require_actor(user('customer'), CUSTOMER)

    def test_superuser_passes_every_actor(self):
        sudo = user('sudo', branch_id=None, organization_id=None, superuser=True)
        for actor in (ADMIN, STAFF, CUSTOMER):
            require_actor(sudo, actor)
            require_branch_actor(FakeConnection(), BRANCH, sudo, **{actor: True})


class BranchScopeTests(unittest.TestCase):
    def test_customer_must_belong_to_branch_organization(self):
        connection = FakeConnection()
        require_branch_actor(connection, BRANCH, user('customer'), customer=True)
        with self.assertRaises(HTTPException) as failure:
            require_branch_actor(connection, BRANCH, user('customer', organization_id='org-2'),
                                 customer=True)
        self.assertEqual(failure.exception.status_code, 403)

    def test_staff_must_match_branch(self):
        connection = FakeConnection()
        require_branch_actor(connection, BRANCH, user('employee'), staff=True)
        for bad in (user('employee', branch_id=OTHER_BRANCH),
                    user('staff', organization_id='org-2')):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException):
                    require_branch_actor(connection, BRANCH, bad, staff=True)

    def test_admin_uses_existing_branch_policy(self):
        connection = FakeConnection()
        require_branch_actor(connection, BRANCH, user('owner'), admin=True)
        require_branch_actor(connection, BRANCH, user('manager'), admin=True)
        with self.assertRaises(HTTPException):
            require_branch_actor(connection, OTHER_BRANCH, user('manager'), admin=True)

    def test_branch_not_found(self):
        original = FakeConnection.execute

        def empty(_self, _sql, _params):
            return FakeCursor(None)

        FakeConnection.execute = empty
        try:
            with self.assertRaises(HTTPException) as failure:
                require_branch_actor(FakeConnection(), BRANCH, user('customer'), customer=True)
            self.assertEqual(failure.exception.status_code, 404)
        finally:
            FakeConnection.execute = original

    def test_role_is_checked_before_database(self):
        with self.assertRaises(HTTPException) as failure:
            require_branch_actor(FakeConnection(), BRANCH, user('customer'), admin=True)
        self.assertEqual(failure.exception.status_code, 403)


if __name__ == '__main__':
    unittest.main()
