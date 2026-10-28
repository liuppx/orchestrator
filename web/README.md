# Web

React + Vite control-plane UI.

From the repository root:

```bash
cd web
npm ci
npm run dev
```

The development server is available at `http://127.0.0.1:5174/` and proxies `/api` to the backend on port 3900. Build production assets with `npm run build`; output is written to `web/dist/` and served by the Python backend.
