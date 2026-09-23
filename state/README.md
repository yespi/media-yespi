# Carpeta `state/` — tu configuración (no está en Git)

Todo lo que **personalizas una vez** vive aquí. `git pull` y las actualizaciones del repo **no tocan** esta carpeta.

Estructura tras `scripts/init-state.sh`:

```text
state/
  .env                 # SESSION_SECRET, PORTAL_BRAND, …
  config/
    users.json         # hash de contraseña admin
    logo.png           # opcional
  data/gopro/
    videos/
    photos/
```

**Primera vez:** `./scripts/init-state.sh`  
**Actualizar código:** `./scripts/upgrade.sh` (pull + rebuild; no borra `state/`)

En Portainer: variable `STATE_DIR=/ruta/absoluta/state` apuntando a esta carpeta.
