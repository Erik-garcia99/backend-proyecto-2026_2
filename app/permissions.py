"""Roles y alcance por sucursal para la API.

Los nombres de rol son los que ya existen en la base de datos; no se crean
roles nuevos. Actores de la arquitectura: A = administración, E = empleado,
C = cliente. SUDO conserva el acceso que ya tiene en el sistema actual.
"""
from fastapi import HTTPException

from .registration_tokens import require_branch_access

ADMIN_ROLES = frozenset({'owner', 'manager', 'rh', 'gerencia'})
STAFF_ROLES = frozenset({'employee', 'staff'})
CUSTOMER_ROLES = frozenset({'customer'})

# POS (decisión del operador 23/11): vender no es un rol, es un permiso
# otorgado por persona. SUDO y owner siempre pueden vender y otorgar;
# RRHH otorga/revoca pero no vende salvo que también se le otorgue.
POS_SELLER_ALWAYS_ROLES = frozenset({'owner'})
POS_SELLER_GRANTOR_ROLES = frozenset({'owner', 'rh'})

ADMIN = 'admin'
STAFF = 'staff'
CUSTOMER = 'customer'


def role(user):
    """Rol normalizado del usuario; nunca lanza si falta el campo."""
    return (user.get('role') or '').lower()


def is_superuser(user):
    return user.get('is_superuser') is True or role(user) == 'sudo'


def is_admin(user):
    """A: dueño/encargado y personal administrativo, más SUDO."""
    return is_superuser(user) or role(user) in ADMIN_ROLES


def can_edit_map(user):
    """Distribución del local: RRHH no edita, aunque administre otros módulos."""
    return is_superuser(user) or role(user) in {'owner', 'manager', 'gerencia'}


def is_staff(user):
    """E: empleado/personal sin funciones administrativas."""
    return role(user) in STAFF_ROLES


def is_customer(user):
    """C: cliente de la app móvil."""
    return role(user) in CUSTOMER_ROLES


def can_sell_pos_by_role(user):
    """SUDO y owner venden siempre; los demás necesitan otorgamiento."""
    return is_superuser(user) or role(user) in POS_SELLER_ALWAYS_ROLES


def can_grant_pos_sellers(user):
    """Puede otorgar/revocar vendedores del POS; RRHH hace la doble verificación."""
    return is_superuser(user) or role(user) in POS_SELLER_GRANTOR_ROLES


def require_actor(user, *allowed):
    """403 si el rol no está entre los actores permitidos. SUDO siempre pasa."""
    if is_superuser(user):
        return
    current = role(user)
    if ADMIN in allowed and current in ADMIN_ROLES:
        return
    if STAFF in allowed and current in STAFF_ROLES:
        return
    if CUSTOMER in allowed and current in CUSTOMER_ROLES:
        return
    raise HTTPException(403, 'No tienes permisos para esta acción')


def require_branch_actor(connection, branch_id, user, *, admin=False, staff=False, customer=False):
    """Autoriza rol y sucursal a la vez; devuelve la fila activa de ``branch``.

    - A: política vigente de ``require_branch_access`` (dueño/gerencia en la
      organización; manager/rh solo en su sucursal; SUDO sin restricción).
    - E: misma organización y misma sucursal.
    - C: sucursal de su organización, sin pertenencia a la sucursal (D-C16).
    """
    allowed = tuple(name for name, flag in
                    ((ADMIN, admin), (STAFF, staff), (CUSTOMER, customer)) if flag)
    require_actor(user, *allowed)
    branch = connection.execute(
        '''SELECT b.id, b.organization_id, b.name, b.address, b.max_capacity_people, b.occupancy_near_pct FROM branch b
           JOIN organization o ON o.id=b.organization_id
           WHERE b.id=%s AND b.is_active AND o.is_active''',
        (branch_id,)).fetchone()
    if not branch:
        raise HTTPException(404, 'Sucursal no encontrada o inactiva')
    if is_superuser(user):
        return branch
    if is_customer(user):
        if branch['organization_id'] != user.get('organization_id'):
            raise HTTPException(403, 'No tienes acceso a esta sucursal')
        return branch
    if is_staff(user):
        if (user.get('organization_id') != branch['organization_id']
                or str(user.get('branch_id')) != str(branch_id)):
            raise HTTPException(403, 'No tienes acceso a esta sucursal')
        return branch
    require_branch_access(user, branch_id, branch['organization_id'])
    return branch
