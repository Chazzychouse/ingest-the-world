# OIDC configuration

The API and SPA are provider-neutral. Everything that differs between identity providers
is configuration, and the SPA gets its half from `GET /config` at runtime, so one build
serves all three.

## Token contract

The API accepts a JWT access token when:

- the signature verifies against the issuer's JWKS (RS256 or ES256 only; `alg` is checked
  before any key lookup),
- `iss` equals `INGEST_OIDC_ISSUER` and `aud` contains `INGEST_OIDC_AUDIENCE`,
- `exp`, `iat` and `sub` are present (60 s clock leeway).

Authorization reads three claims, each renameable and allowed to be a dotted path:

| Setting | Default | Meaning |
|---------|---------|---------|
| `INGEST_OIDC_ROLES_CLAIM` | `roles` | `Reader` < `Contributor` < `Curator` < `Admin`, plus `Clearance.<MARKING>` roles |
| `INGEST_OIDC_GROUPS_CLAIM` | `groups` | Matched against `principal_type = 'group'` collection grants |
| `INGEST_OIDC_CLEARANCE_CLAIM` | `clearance` | Alternative to `Clearance.*` roles, e.g. a directory attribute |

Every principal implicitly holds `UNCLASSIFIED`. Read access to a collection needs
**both** a clearance that covers its marking **and** a grant (Admins skip the grant, never
the clearance). The rule lives in one SQL function, `authz.readable_collections`.

## Per-provider settings

| Setting | Keycloak (Compose) | Entra ID, commercial | Entra ID, Azure Government |
|---------|--------------------|----------------------|----------------------------|
| `INGEST_OIDC_ISSUER` | `http://localhost:8080/realms/ingest` | `https://login.microsoftonline.com/<tenant>/v2.0` | `https://login.microsoftonline.us/<tenant>/v2.0` |
| `INGEST_OIDC_AUDIENCE` | `ingest-api` | API app's client id | API app's client id |
| `INGEST_OIDC_JWKS_URI` | `http://keycloak:8080/...` (container network) | unset (discovered) | unset (discovered) |
| `INGEST_WEB_OIDC_CLIENT_ID` | `ingest-web` | SPA app's client id | SPA app's client id |
| `INGEST_WEB_OIDC_SCOPE` | `openid profile email` | `openid profile email api://<api client id>/access_as_user` | same as commercial |

Terraform prints the Entra values as the `api_settings` output.

## Entra ID notes

- The API app registration sets `requested_access_token_version = 2`. Without it Entra
  issues v1 tokens whose `iss` is `https://sts.windows.net/<tenant>/` and whose `aud` is the
  app id URI, and validation fails.
- Use **app roles**, not group claims, for authorization. The `groups` claim is replaced
  by an overage pointer once a user is in more than 200 groups (research doc 02 §5).
- Clearance is the `Clearance.CUI` app role. A directory attribute through optional
  claims works too: point `INGEST_OIDC_CLEARANCE_CLAIM` at it.
- `app_role_assignment_required = true` on the API's service principal: users with no
  assigned role can't get a token for it at all.
