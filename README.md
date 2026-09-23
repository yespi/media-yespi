# media-yespi

Portal web **autohospedado** para bibliotecas de **vídeo** (GoPro, MP4…) y **fotos**: carpetas, miniaturas (ffmpeg), reproducción en el navegador, búsqueda y ordenación.

Sin base de datos: todo son ficheros en disco.

## Inicio rápido (Docker)

```bash
git clone https://github.com/yespi/media-yespi.git
cd media-yespi
cp .env.example .env
cp config/users.json.example config/users.json
# Edita .env y config/users.json según el modo de auth (abajo)
mkdir -p data/gopro/videos data/gopro/photos
docker compose build
docker compose up -d
```

Abre `http://IP-DEL-SERVIDOR:8098`.

## Modos de autenticación

| Modo | Variables `.env` | Visitantes | Administrador |
|------|------------------|------------|---------------|
| **A — Abierto** | `AUTH_DISABLED=1` | Todo (incl. borrar) | — |
| **B — Público + admin local** (recomendado en casa) | `AUTH_DISABLED=0`, `AUTH_PUBLIC_READ=1`, `AUTH_LOCAL=1` | Ver vídeos/fotos **sin login** | Usuario/clave en `config/users.json`, botón «Entrar (admin)», puede **borrar** |
| **C — Google OAuth** | `AUTH_DISABLED=0`, `AUTH_PUBLIC_READ=0` o `1`, `AUTH_LOCAL=0` | Solo si `AUTH_PUBLIC_READ=1` | Login Google; correos en `users.json` |
| **D — Google + lectura pública** | `AUTH_PUBLIC_READ=1`, `AUTH_LOCAL=0`, credenciales Google | Anónimos ven; borrar con Google + `admin: true` | |

No hace falta Google en el **modo B**.

### Modo B — ejemplo `config/users.json`

```json
{
  "users": [
    {
      "username": "admin",
      "name": "Administrador",
      "admin": true,
      "password_hash": "pbkdf2_sha256$..."
    }
  ]
}
```

Generar hash:

```bash
python3 hash_password.py 'tu-contraseña-segura'
```

El ejemplo en `config/users.json.example` usa la contraseña de demostración **`CambiarEstaClave`** (cámbiala antes de exponer el portal).

### Modo C/D — Google Cloud

1. [Credenciales OAuth](https://console.cloud.google.com/apis/credentials) → cliente web.
2. URI de redirección: la misma que `GOOGLE_REDIRECT_URI` en `.env` (p. ej. `https://videos.tudominio/auth/google/callback`).
3. En `config/users.json`, lista blanca por `email` (y `"admin": true` para borrar si usas `AUTH_PUBLIC_READ=1`).

## Datos en disco

Por defecto `MEDIA_ROOT=/data/gopro` en el contenedor:

```text
data/gopro/
  videos/     ← vídeos (subcarpetas OK)
  photos/     ← fotos
  thumbs/     ← generado
```

En `docker-compose.yml` puedes montar otra ruta del host (p. ej. `/srv/guardianes` → `/data/guardianes` y `MEDIA_ROOT=/data/guardianes` en `environment`).

## Marca y logo

En `.env`:

```env
PORTAL_BRAND=Mi biblioteca
```

Logo opcional: `config/logo.png` (se sirve en `/api/logo`) o `PORTAL_LOGO_URL=https://...`.

## Portainer

- Descomprime o clona el repo en la Pi.
- Variable `MEDIA_PORTAL_ROOT=/ruta/al/repo`.
- No pegues solo el YAML sin los ficheros: hace falta `app.py`, `Dockerfile`, `config/`, etc.
- Si falla el build: `docker compose -f docker-compose.nobuild.yml up -d`.

## Actualizar

```bash
cd media-yespi
git pull
docker compose build
docker compose up -d
```

Conserva tu `.env` y `config/users.json` (no están en git).

## Portainer / «GOOGLE_CLIENT_ID required» / reinicio en bucle

El contenedor necesita **modo B** en las variables de entorno:

| Variable | Valor |
|----------|--------|
| `AUTH_DISABLED` | `0` |
| `AUTH_PUBLIC_READ` | `1` |
| `AUTH_LOCAL` | `1` |
| `SESSION_SECRET` | cadena larga (`openssl rand -hex 32`) |

En **Portainer → Stack → Environment**, añádelas explícitamente (no confíes solo en sustitución `${...}` del YAML). Sin `AUTH_LOCAL=1` el arranque exige Google y el contenedor entra en **Restarting**.

`hash_password.py` está en la **raíz del repo** (junto a `app.py`), no dentro de `config/`.

## Licencia

MIT — ver [LICENSE](LICENSE).
