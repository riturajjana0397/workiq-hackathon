import jwt, asyncio
from azure.identity.aio import AzureCliCredential
async def main():
    cred = AzureCliCredential()
    tok = await cred.get_token('https://ai.azure.com/.default')
    claims = jwt.decode(tok.token, options={'verify_signature': False})
    print('tid (tenant in token):', claims.get('tid'))
    print('aud (audience):       ', claims.get('aud'))
    print('upn / appid:          ', claims.get('upn') or claims.get('appid'))
    await cred.close()

asyncio.run(main())