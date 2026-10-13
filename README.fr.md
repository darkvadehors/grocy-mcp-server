# grocy-mcp-server

*[English version](README.md)*

Un serveur [MCP](https://modelcontextprotocol.io) qui donne à Claude et ChatGPT un accès personnel à une instance [Grocy](https://grocy.info). Chaque personne se connecte avec **sa propre clé API Grocy** : chaque action est donc enregistrée dans Grocy sous son propre compte.

Construit sur le SDK Python officiel de MCP (`mcp==2.3.0`) : le même processus est à la fois le serveur d'autorisation OAuth 2.1 et la ressource protégée `/mcp`.

## Sécurité

- **OAuth 2.1** avec PKCE S256 obligatoire, code d'autorisation lié au `resource`, `iss` dans la redirection.
- **Enregistrement des clients** (DCR) limité aux URI de retour officielles de Claude et ChatGPT, à la boucle locale (port libre) et à `MCP_EXTRA_REDIRECT_URIS` ; documents de métadonnées client (CIMD) récupérés uniquement depuis `MCP_CIMD_HOSTS`, sans redirection, taille plafonnée, adresses non publiques refusées. Les noms de fournisseurs (Claude, ChatGPT, Anthropic, OpenAI) sont réservés.
- **Clé Grocy** collée par la personne sur la page `/connexion` : vérifiée par `GET /api/user`, refusée si elle appartient à un administrateur, chiffrée en AES-GCM (clé maître chargée depuis un identifiant systemd), déchiffrée seulement le temps d'un appel à Grocy, jamais journalisée, jamais renvoyée.
- **Jetons** de 256 bits, stockés uniquement sous forme d'empreinte SHA-256, à usage unique, remplacés à chaque renouvellement ; le rejeu d'un code ou d'un jeton déjà utilisé révoque toute la connexion. Durées : accès 1 h, renouvellement 30 jours glissants (7 jours pour la boucle locale), connexion 90 jours au plus.
- **Garde-fous sur les actions** : quantité plafonnée par appel, confirmation en deux temps pour les grosses corrections d'inventaire et toujours pour l'ajout des manquants (aperçu puis jeton), quotas (30/min, 300/h par compte), clés d'idempotence (10 min), annulation de la dernière action (< 24 h). **Aucun outil de suppression ni de modification** (produits, emplacements et catégories peuvent seulement être créés). Aucun outil ne prend d'identifiant d'utilisateur.
- **Journal d'audit** en JSON Lines, chaîné par empreinte, secrets masqués.
- **Pages** sans JavaScript, CSP stricte, cookies `__Host-`, jetons anti-falsification ; transport protégé contre les `Origin` étrangers et les `Host` inattendus (DNS rebinding).
- Une clé Grocy révoquée dans Grocy révoque les connexions correspondantes ; une panne ou une lenteur de Grocy ne déconnecte jamais personne.

## Outils

| Outil | Rôle | Type |
|---|---|---|
| `rechercher_produit` | Trouver un produit par nom ou code-barres | lecture |
| `voir_stock` | Stock actuel, éventuellement pour un emplacement | lecture |
| `voir_peremptions` | Produits périmés, en retard ou bientôt à consommer | lecture |
| `voir_produit` | Détails, lots et historique des prix d'un produit | lecture |
| `voir_liste_courses` | Liste de courses | lecture |
| `recettes_faisables` | Recettes réalisables avec le stock actuel | lecture |
| `voir_references` | Emplacements et unités | lecture |
| `ajouter_liste_courses` | Ajouter un produit à la liste de courses | action |
| `retirer_liste_courses` | Retirer un produit de la liste de courses | action |
| `ajouter_manquants_liste` | Ajouter les manquants (sous le minimum ou pour une recette) | action |
| `enregistrer_achat` | Enregistrer un achat | action |
| `consommer` | Consommer (ou jeter) un produit | action |
| `ouvrir` | Marquer un produit comme ouvert | action |
| `deplacer` | Déplacer un produit d'un emplacement à un autre | action |
| `corriger_inventaire` | Indiquer la quantité réelle d'un produit | action |
| `importer_articles` | Importer une liste d'articles scannés (par exemple une sauvegarde FoodShiner) en deux temps : aperçu, puis confirmation ; les codes-barres inconnus sont identifiés par la recherche externe de Grocy | action |
| `creer_emplacement` | Créer un emplacement | action |
| `creer_categorie` | Créer une catégorie (groupe de produits) | action |
| `annuler_derniere_action` | Annuler la dernière action faite par ce serveur | action |

## Prérequis Grocy

Créer **un compte Grocy dédié, non administrateur, par personne** (par exemple `alice-ia`). Grocy donne la permission `ADMIN` aux nouveaux comptes : la retirer. N'accorder que :

`STOCK_PURCHASE`, `STOCK_CONSUME`, `STOCK_INVENTORY`, `STOCK_TRANSFER`, `STOCK_OPEN`, `STOCK_EDIT` (nécessaire à l'annulation), `SHOPPINGLIST_ITEMS_ADD`, `SHOPPINGLIST_ITEMS_DELETE`, `RECIPES`, `MASTER_DATA_EDIT` (création de produits, d'emplacements et de catégories).

À savoir : dans Grocy, `MASTER_DATA_EDIT` permet aussi de modifier et de supprimer produits, emplacements et catégories — il n'existe pas de permission de création seule. Ce serveur n'expose aucun outil qui les modifie ou les supprime, mais une clé volée d'un tel compte le pourrait.

Créer ensuite une clé API pour ce compte (Grocy → menu de l'utilisateur → Gérer les clés API).

## Configuration

| Variable | Rôle |
|---|---|
| `GROCY_BASE_URL` | Adresse de Grocy, `https://` uniquement, sans chemin |
| `MCP_PUBLIC_URL` | Adresse publique de ce serveur, `https://` uniquement, sans chemin |
| `MCP_DB_PATH` | Base SQLite |
| `MCP_AUDIT_PATH` | Journal d'audit (JSON Lines) |
| `CREDENTIALS_DIRECTORY` | Fourni par `LoadCredential=` de systemd ; la clé maître est lue dans `mcp-key` |
| `MCP_KEY_FILE` | Fichier de clé maître (développement et tests) |
| `MCP_MAX_QUANTITY` | Unités maximum par appel (50) |
| `MCP_INVENTORY_ABS_THRESHOLD`, `MCP_INVENTORY_PCT_THRESHOLD` | Seuils de confirmation d'inventaire (10 unités, 50 %) |
| `MCP_ACTIONS_PER_MINUTE`, `MCP_ACTIONS_PER_HOUR` | Quotas par compte (30, 300) |
| `MCP_MAX_CLIENTS`, `MCP_MAX_CONNECTIONS_PER_ACCOUNT` | Clients enregistrés (200), connexions actives par compte (10) |
| `MCP_CIMD_HOSTS` | Hôtes autorisés pour les documents CIMD (`claude.ai chatgpt.com`) |
| `MCP_EXTRA_REDIRECT_URIS` | URI de retour supplémentaires, séparées par des espaces |
| `MCP_ALLOWED_ORIGINS` | Valeurs d'`Origin` autorisées sur `/mcp` |
| `MCP_TIMEZONE` | Fuseau horaire des personnes (`UTC` par défaut), par exemple `Europe/Zurich` |

Clé maître :

```bash
python -m grocy_mcp generate-key /etc/grocy-mcp/mcp-key          # 32 octets aléatoires, fichier 0600
python -m grocy_mcp rotate-key /etc/grocy-mcp/mcp-key /etc/grocy-mcp/mcp-key.new
```

## Déploiement type

- Une unité systemd `.socket` qui écoute sur une socket Unix et une unité `.service` qu'elle démarre (`python -m grocy_mcp serve`) : utilisateur dédié, `LoadCredential=mcp-key:/etc/grocy-mcp/mcp-key`, `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateDevices=yes`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `IPAddressDeny=localhost`, `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute=yes`.
- nginx termine TLS pour `mcp.example.org` et relaie `/mcp`, `/.well-known/`, `/authorize`, `/token`, `/register`, `/revoke`, `/connexion`, `/connexions` et `/style.css` vers la socket Unix. Le service n'ouvre aucun port TCP.
- Installer les dépendances avec `pip install --require-hashes -r requirements.lock`.

## Ajouter le connecteur

- **Claude** : Paramètres → Connecteurs → Ajouter un connecteur personnalisé → `https://mcp.example.org/mcp`. Claude ouvre la page `/connexion` : coller sa clé API Grocy, puis confirmer.
- **ChatGPT** : Paramètres → Applications → mode développeur → créer un connecteur avec l'adresse `https://mcp.example.org/mcp` et l'authentification OAuth.

Les connexions se consultent et se révoquent sur `https://mcp.example.org/connexions`.

## Développement

```bash
uv venv -p python3.14 .venv
uv pip install -p .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest
uvx ruff check src tests
```

Les tests utilisent un Grocy simulé en mémoire (`tests/fake_grocy.py`) ; aucun accès réseau n'est nécessaire.

## Licence

MIT
