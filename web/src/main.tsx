import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { AuthProvider } from 'react-oidc-context'
import { WebStorageStateStore } from 'oidc-client-ts'
import './index.css'
import App from './App.tsx'

type WebConfig = { oidc: { authority: string; client_id: string; scope: string } }

const root = createRoot(document.getElementById('root')!)

// IdP settings come from the API at runtime, so one build serves Keycloak, commercial
// Entra ID and Azure Government Entra ID.
async function boot() {
  const response = await fetch('/api/config')
  if (!response.ok) throw new Error(`GET /api/config failed: ${response.status}`)
  const { oidc }: WebConfig = await response.json()

  root.render(
    <StrictMode>
      <AuthProvider
        authority={oidc.authority}
        client_id={oidc.client_id}
        scope={oidc.scope}
        redirect_uri={`${window.location.origin}/`}
        post_logout_redirect_uri={`${window.location.origin}/`}
        userStore={new WebStorageStateStore({ store: window.sessionStorage })}
        onSigninCallback={() => window.history.replaceState({}, document.title, '/')}
      >
        <App />
      </AuthProvider>
    </StrictMode>,
  )
}

boot().catch((error: Error) => {
  root.render(
    <main className="fatal">
      <h1>Can't reach the platform API</h1>
      <p>{error.message}</p>
    </main>,
  )
})
