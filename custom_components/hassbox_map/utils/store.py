from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.storage import Store

STORE_VERSION = 1

def get_store_for_key(hass, key):

    return Store(hass, STORE_VERSION, key, encoder=JSONEncoder, atomic_writes=True)

async def async_load_from_store(hass, key):

    return await get_store_for_key(hass, key).async_load() or {}

async def async_save_to_store(hass, key, data):

    await get_store_for_key(hass, key).async_save(data)

async def async_remove_store(hass, key):

    await get_store_for_key(hass, key).async_remove()
