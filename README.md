# grocy-mcp-server

*[Version française](README.fr.md)*

An [MCP](https://modelcontextprotocol.io) server that gives Claude and ChatGPT personal access to a [Grocy](https://grocy.info) instance. Each person connects with **their own Grocy API key**, so every action is recorded in Grocy under their own account.

Built on the official MCP Python SDK (`mcp==2.3.0`): the same process is both the OAuth 2.1 authorization server and the protected `/mcp` resource.

## Security

- **OAuth 2.1** with mandatory PKCE S256, authorization code bound to the `resource`, `iss` in the redirect.
- **Client registration** (DCR) limited to the official redirect URIs of Claude and ChatGPT, local loopback (any port) and `MCP_EXTRA_REDIRECT_URIS`; Client ID Metadata Documents (CIMD) fetched only from `MCP_CIMD_HOSTS`, without redirects, size-capped, non-public addresses refused. Provider names (Claude, ChatGPT, Anthropic, OpenAI) are reserved.
- **Grocy key** pasted by the person on the `/connexion` page: checked with `GET /api/user`, refused if it belongs to an administrator, encrypted with AES-GCM (master key loaded from a systemd credential), decrypted only for the duration of a Grocy call, never logged, never returned.
- **Tokens** of 256 bits, stored only as SHA-256 hashes, single use, rotated on every refresh; replaying a used code or refresh token revokes the whole connection. Lifetimes: access 1 h, refresh 30 days (sliding, 7 days for loopback clients), connection 90 days at most.
- **Guardrails on actions**: per-call quantity cap, two-step confirmation for large inventory corrections and always for adding missing products (preview, then token), rate limits (30/min, 300/h per account), idempotency keys (10 min), undo of the last action (< 24 h). **No delete or edit tool** (products, locations and product groups can only be created). No tool takes a user identifier.
- **Audit log** in JSON Lines, hash-chained, secrets masked.
- **Pages** without JavaScript, strict CSP, `__Host-` cookies, CSRF tokens; transport protected against foreign `Origin` and unexpected `Host` (DNS rebinding).
- A Grocy key revoked in Grocy revokes the matching connections; a Grocy outage or timeout never disconnects anyone.

## Tools

| Tool | Purpose | Kind |
|---|---|---|
| `rechercher_produit` | Find a product by name or barcode | read |
| `voir_stock` | Current stock, optionally for one location | read |
| `voir_peremptions` | Expired, overdue and soon-due products | read |
| `voir_produit` | Details, batches and price history of a product | read |
| `voir_liste_courses` | Shopping list | read |
| `recettes_faisables` | Recipes that can be cooked with the current stock | read |
| `voir_references` | Locations and quantity units | read |
| `ajouter_liste_courses` | Add a product to the shopping list | action |
| `retirer_liste_courses` | Remove a product from the shopping list | action |
| `ajouter_manquants_liste` | Add missing products (below minimum, or for a recipe) | action |
| `enregistrer_achat` | Record a purchase | action |
| `consommer` | Consume (or throw away) a product | action |
| `ouvrir` | Mark a product as opened | action |
| `deplacer` | Move a product between locations | action |
| `corriger_inventaire` | Set the real quantity of a product | action |
| `importer_articles` | Import a list of scanned items (e.g. a FoodShiner backup) in two steps: preview, then confirmation; unknown barcodes are identified with Grocy's external barcode lookup | action |
| `creer_emplacement` | Create a location | action |
| `creer_categorie` | Create a product group | action |
| `annuler_derniere_action` | Undo the last action made through this server | action |

Tool names and messages are in French.

## Grocy prerequisites

Create **one dedicated, non-administrator Grocy account per person** (for example `alice-ia`). Grocy gives new accounts the `ADMIN` permission by default: remove it. Grant only:

`STOCK_PURCHASE`, `STOCK_CONSUME`, `STOCK_INVENTORY`, `STOCK_TRANSFER`, `STOCK_OPEN`, `STOCK_EDIT` (needed to undo), `SHOPPINGLIST_ITEMS_ADD`, `SHOPPINGLIST_ITEMS_DELETE`, `RECIPES`, `MASTER_DATA_EDIT` (creating products, locations and product groups).

Note: in Grocy, `MASTER_DATA_EDIT` also allows editing and deleting products, locations and product groups — there is no create-only permission. This server exposes no tool that edits or deletes them, but a stolen key of such an account could.

Then create an API key for that account (Grocy → user menu → Manage API keys).

## Configuration

| Variable | Meaning |
|---|---|
| `GROCY_BASE_URL` | Grocy address, `https://` only, no path |
| `MCP_PUBLIC_URL` | Public address of this server, `https://` only, no path |
| `MCP_DB_PATH` | SQLite database |
| `MCP_AUDIT_PATH` | Audit log (JSON Lines) |
| `CREDENTIALS_DIRECTORY` | Set by systemd `LoadCredential=`; the master key is read from `mcp-key` |
| `MCP_KEY_FILE` | Master key file (development and tests) |
| `MCP_MAX_QUANTITY` | Max units per call (50) |
| `MCP_INVENTORY_ABS_THRESHOLD`, `MCP_INVENTORY_PCT_THRESHOLD` | Inventory confirmation thresholds (10 units, 50 %) |
| `MCP_ACTIONS_PER_MINUTE`, `MCP_ACTIONS_PER_HOUR` | Rate limits per account (30, 300) |
| `MCP_MAX_CLIENTS`, `MCP_MAX_CONNECTIONS_PER_ACCOUNT` | Registered clients (200), active connections per account (10) |
| `MCP_CIMD_HOSTS` | Hosts allowed for CIMD documents (`claude.ai chatgpt.com`) |
| `MCP_EXTRA_REDIRECT_URIS` | Additional redirect URIs, space separated |
| `MCP_ALLOWED_ORIGINS` | Allowed `Origin` values on `/mcp` |

Master key:

```bash
python -m grocy_mcp generate-key /etc/grocy-mcp/mcp-key          # 32 random bytes, file 0600
python -m grocy_mcp rotate-key /etc/grocy-mcp/mcp-key /etc/grocy-mcp/mcp-key.new
```

## Typical deployment

- A systemd `.socket` unit listening on a Unix socket, and the `.service` unit it starts (`python -m grocy_mcp serve`): dedicated user, `LoadCredential=mcp-key:/etc/grocy-mcp/mcp-key`, `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateDevices=yes`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `IPAddressDeny=localhost`, `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute=yes`.
- nginx terminates TLS for `mcp.example.org` and proxies `/mcp`, `/.well-known/`, `/authorize`, `/token`, `/register`, `/revoke`, `/connexion`, `/connexions` and `/style.css` to the Unix socket. The service opens no TCP port.
- Install the dependencies with `pip install --require-hashes -r requirements.lock`.

## Adding the connector

- **Claude**: Settings → Connectors → Add custom connector → `https://mcp.example.org/mcp`. Claude opens the `/connexion` page: paste your Grocy API key, then confirm.
- **ChatGPT**: Settings → Apps → developer mode → create a connector with the URL `https://mcp.example.org/mcp` and OAuth authentication.

Connections can be listed and revoked at `https://mcp.example.org/connexions`.

## Development

```bash
uv venv -p python3.14 .venv
uv pip install -p .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest
uvx ruff check src tests
```

The tests use an in-memory Grocy simulator (`tests/fake_grocy.py`); no network access is needed.

## License

MIT
