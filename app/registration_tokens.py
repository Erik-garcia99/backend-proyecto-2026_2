import asyncio
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from .schemas import BusinessTokenResponse, BranchTokenResponse
from .security import generate_registration_token, hash_registration_token

def require_branch_access(user, branch_id, organization_id):
    role=user['role'].lower()
    if user['is_superuser']:return
    if user['organization_id']==organization_id and (role in {'owner','gerencia'} or (role in {'manager','rh'} and user['branch_id']==branch_id)):
        return
    raise HTTPException(403,'No tienes permisos sobre esta sucursal')

def validate_branch_token(get_connection,branch_id,digest):
    with get_connection() as connection:
        return connection.execute('''SELECT 1 FROM organization_token t
            JOIN branch b ON b.id=t.branch_id AND b.organization_id=t.organization_id
            JOIN organization o ON o.id=b.organization_id
            WHERE t.branch_id=%s AND t.token_hash=%s AND t.is_active AND t.revoked_at IS NULL
            AND b.is_active AND o.is_active AND 'register_gateway'=ANY(t.scopes)
            AND (t.expires_at IS NULL OR t.expires_at>NOW()) LIMIT 1''',(branch_id,digest)).fetchone() is not None

def create_router(current_user,get_connection,hub):
    router=APIRouter(tags=['Registration tokens'])

    @router.post('/businesses/{business_id}/token',response_model=BusinessTokenResponse)
    def issue_business_token(business_id:UUID,response:Response,user=Depends(current_user)):
        if not user['is_superuser']:raise HTTPException(403,'Solo SUDO puede emitir el token global')
        token=generate_registration_token()
        with get_connection() as connection:
            if not connection.execute('SELECT id FROM organization WHERE id=%s AND is_active FOR NO KEY UPDATE',(business_id,)).fetchone():
                raise HTTPException(404,'Empresa inexistente o inactiva')
            connection.execute('UPDATE business_registration_token SET is_active=FALSE WHERE business_id=%s',(business_id,))
            connection.execute('INSERT INTO business_registration_token(business_id,token_hash,created_by) VALUES(%s,%s,%s)',
                (business_id,hash_registration_token(token),user['id']))
            connection.commit()
        response.headers['Cache-Control']='no-store'
        return BusinessTokenResponse(business_id=str(business_id),business_token=token)

    def replace_branch_token(branch_id,user):
        token=generate_registration_token()
        with get_connection() as connection:
            branch=connection.execute('''SELECT b.id,b.organization_id FROM branch b JOIN organization o ON o.id=b.organization_id
                WHERE b.id=%s AND b.is_active AND o.is_active FOR NO KEY UPDATE OF b''',(branch_id,)).fetchone()
            if not branch:raise HTTPException(404,'Sucursal inexistente o inactiva')
            require_branch_access(user,branch_id,branch['organization_id'])
            connection.execute('UPDATE organization_token SET is_active=FALSE,revoked_at=NOW() WHERE branch_id=%s',(branch_id,))
            connection.execute('''INSERT INTO organization_token(organization_id,branch_id,token_hash,token_prefix,created_by)
                VALUES(%s,%s,%s,%s,%s)''',(branch['organization_id'],branch_id,hash_registration_token(token),token[:8],user['id']))
            connection.commit()
        return token

    @router.post('/branches/{branch_id}/token',response_model=BranchTokenResponse)
    async def issue_branch_token(branch_id:UUID,response:Response,user=Depends(current_user)):
        operation=asyncio.create_task(asyncio.to_thread(replace_branch_token,branch_id,user))
        try: token=await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            await hub.disconnect_branch(branch_id)
            raise
        await hub.disconnect_branch(branch_id)
        response.headers['Cache-Control']='no-store'
        return BranchTokenResponse(branch_id=str(branch_id),branch_token=token)
    return router