from hashlib import blake2b

def short_rid(uuid_str: str) -> str:
    h = blake2b(uuid_str.encode(), digest_size=3).hexdigest().upper()
    return f"RID-{h}"

def is_owner(user_id: int, owner_id_env: str) -> bool:
    try:
        return int(owner_id_env) == user_id
    except Exception:
        return False
