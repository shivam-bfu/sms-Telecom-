# SMS Integrator - Cloud Control Panel & Router

This is a modern, modular, production-grade Python Flask backend for the **SMS Integrator** system. It provides:
1. **Admin Console (`/admin`)**: Monitor node heartbeats (battery, status, last-seen, version), manage authorized user accounts, and audit all incoming/outgoing messages.
2. **User Portal (`/`)**: A sleek PWA dashboard where users can log in, send outgoing SMS through your company integrator node, and view their personal message threads.
3. **Integrator Router APIs (`/api/v1`)**: Handles secure polling, status callbacks, and duplicate message routing for the Android client node.

---

## 🚀 Quick Start (Local Run)

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables (Optional)**:
   Create a `.env` file (or set them in your terminal):
   ```bash
   export ADMIN_PASSWORD="your_admin_password_here"
   export DEVICE_TOKEN="your_secure_device_secret_here"
   export MY_NUMBER="+919876543210" # Your phone's SIM number
   ```

3. **Run Local Server**:
   ```bash
   python run.py
   ```
   Open `http://127.0.0.1:5000` for Users or `http://127.0.0.1:5000/admin` for the Admin Console!

---

## ☁️ Deployment

### 1. Fly.io
1. Install [flyctl](https://fly.io/docs/hands-on/install-cli/).
2. Run `fly launch` in this directory.
3. Setup a persistent volume (optional but highly recommended to persist database across restarts):
   ```bash
   fly volumes create sms_gateway_data --size 1
   ```
   Uncomment the `[[mounts]]` section in your generated `fly.toml`:
   ```toml
   [[mounts]]
     source = "sms_gateway_data"
     destination = "/data"
   ```
4. Set secrets:
   ```bash
   fly secrets set ADMIN_PASSWORD="yourpassword" DEVICE_TOKEN="yourtoken" MY_NUMBER="+919876543210"
   ```
5. Deploy: `fly deploy`.

### 2. Railway
1. Push this folder to a GitHub repository.
2. Connect your repository to [Railway](https://railway.app/).
3. Add the following **Variables** in the Railway dashboard:
   - `ADMIN_PASSWORD`
   - `DEVICE_TOKEN`
   - `MY_NUMBER`
   - `DATABASE_PATH` (Set to `/data/sms_gateway.db` and attach a volume, or keep blank to save locally on the ephemeral disk)
4. Railway will automatically detect the `Dockerfile` and deploy!

---

## 🔒 Security Architecture
- **Admin**: Auths via `X-Admin-Password` matching the salted bcrypt hash of your admin password stored in SQLite.
- **Users**: Auths via secure, lightweight Bearer tokens generated upon credentials match.
- **Android Device Node**: Auths via HTTP Basic Auth containing your `DEVICE_TOKEN`.
