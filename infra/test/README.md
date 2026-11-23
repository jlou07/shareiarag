# ShareIARAG : environnement de test isole

Ce dossier deploie un environnement ShareIARAG complet et isole dans un **nouveau** groupe de ressources, sans toucher aux autres environnements. Aucune valeur propre a un tenant ou a un environnement n'est dans le code : tout vient d'un fichier de parametres local, non versionne (`runtime.parameters.json`, voir `runtime.parameters.example.json`). Les scripts utilisent l'abonnement et le tenant selectionnes dans Azure CLI.

Ne lancez aucune commande qui modifie Azure ou Entra sans approbation explicite. Ne reutilisez pas les scripts de deploiement du repertoire `infra` parent : ils ciblent l'environnement initial.

## Convention de nommage

Un seul parametre, `suffix` (3 a 8 lettres minuscules ou chiffres, unique par environnement), derive tous les noms. `main.bicep` et `testenv.py` appliquent la meme convention ; un test unitaire verifie qu'elles restent alignees.

| Element | Nom |
|---|---|
| Search / Foundry / projet | `srch-shareiarag-test-<suffix>`, `aif-shareiarag-test-<suffix>`, `proj-shareiarag-test-<suffix>` |
| Stockage Files / etat | `stshragfiles<suffix>`, `stshragstate<suffix>` |
| Console / MCP / Function | `app-shareiarag-test-console-<suffix>`, `app-shareiarag-test-mcp-<suffix>`, `func-shareiarag-test-dl-<suffix>` |
| Key Vault / identite MCP | `kv-shrag-test-<suffix>`, `id-shareiarag-test-mcp-<suffix>` |
| Inscriptions Entra | `ent-shareiarag-test-console-<suffix>`, `ent-shareiarag-test-mcp-<suffix>`, `ent-shareiarag-test-download-<suffix>` |

Les noms Storage, Key Vault, Search et App Service sont globaux : choisir un suffixe aleatoire, par exemple `python -c "import secrets; print(secrets.token_hex(2))"`. Chaque script accepte `--params <fichier>` ou la variable `SHAREIARAG_TEST_PARAMS` pour gerer plusieurs environnements.

## Deploiement automatise (recommande)

`deploy_test_env.py` enchaine tout le parcours decrit plus bas. Il demande une seule fois de saisir le nom du groupe de ressources, s'arrete a la premiere erreur et reprend a l'etape suivante au prochain lancement (etat dans `.deploy-state-<suffix>.json`, ignore par Git). Chaque redeploiement Bicep passe d'abord par un what-if qui bloque toute suppression et tout changement hors du groupe cible.

```powershell
cd infra\test
az login; az account set --subscription <subscription-id>
# 1. Fichier de parametres avec un suffixe aleatoire dont tous les noms globaux sont libres
python -B deploy_test_env.py init --resource-group <nouveau-groupe> `
  --group-a <groupe-A> --group-b <groupe-B> --tester-a <utilisateur-A> --tester-b <utilisateur-B>
# 2. Apercu, puis execution des 10 etapes
python -B deploy_test_env.py plan
python -B deploy_test_env.py run --execute          # --from <etape> pour rejouer une etape
# 3. Optionnel : connecteur Microsoft 365 Copilot, apres l'inscription SSO du Teams Developer Portal
python -B deploy_test_env.py sso --app-id-uri <application-id-uri> --execute
```

Etapes : `entra`, `deploy`, `entra-post`, `secrets`, `deploy-secrets`, `publish`, `fixtures`, `search`, `foundry`, `smoke`. Restent manuels : l'inscription SSO dans le Teams Developer Portal, le connecteur dans le centre d'administration Microsoft 365 et le role temporaire `Foundry User` pour le Playground. Les sections suivantes detaillent chaque etape pour une execution manuelle ou un diagnostic.

## Couts et limites

- Search S1 (1 unite), plan App Service Linux B2, Function Flex FC1, deux partages Files StandardV2 de 32 Gio, Foundry et supervision isoles.
- Estimation fixe : environ **330 a 355 USD/mois** (France Central), hors modeles, executions, transactions, journaux et taxes. Les couts fixes continuent meme si les applications sont arretees.
- Pas de haute disponibilite, pas de reseau prive ; les endpoints sont publics et proteges par Entra. Les acces documentaires sont accordes **par partage**, pas selon les ACL NTFS de chaque fichier.
- `location` vaut `francecentral` par defaut ; verifier la disponibilite de `gpt-5-mini`, `text-embedding-3-small` et Flex FC1 avant d'en choisir une autre.
- Les inscriptions Entra, leurs consentements et les roles personnalises sont des changements distincts du groupe de ressources.

## Avant la creation

1. Selectionner l'abonnement cible avec `az account set`. Verifier l'absence du groupe choisi, la disponibilite des noms globaux et les quotas. Les capacites modele ne sont pas garanties.
2. Identifier deux groupes de securite existants distincts et deux utilisateurs de test distincts : A membre de A mais pas B, B membre de B mais pas A. Ne modifier aucune appartenance. Verifier aussi les appartenances transitives et les roles RBAC herites : aucun testeur ne doit avoir de privilege de lecture elevee sur le Search test ni etre administrateur de la console test. Le controle CLI des membres peut exiger une appartenance directe.
3. Prevoir un compte deployeur avec les droits de creation des ressources et des attributions de roles, les droits Entra necessaires aux inscriptions et, si la politique du tenant l'impose, un administrateur pour les consentements delegues.
4. Noter les SHA-256 produits par les scripts de paquet. La preparation du bundle console utilise l'esbuild deja installe, sans ecrire dans les sources.
5. Si un nom ou un objet de test existe deja, arreter et l'inspecter : ne pas le reutiliser ou l'ecraser automatiquement. Les controles d'existence des secrets et objets Foundry ne sont pas atomiques ; aucun autre operateur ne doit initialiser simultanement les memes ressources.

## Applications Entra test : apres approbation uniquement

Creer trois nouvelles inscriptions **single-tenant** (`AzureADMyOrg`) avec les noms exacts ci-dessous. Conserver leurs ID d'application et d'objet ; ce ne sont pas des secrets. Ne jamais modifier les inscriptions d'un autre environnement. En cas d'echec ou de timeout Graph, inspecter les objets crees avant de recommencer.

| Inscription | Configuration |
|---|---|
| `ent-shareiarag-test-console-<suffix>` | Exposer `api://<console-client-id>/Console.Access`, jetons d'acces v2. Declarer l'autorisation deleguee `https://search.azure.com/user_impersonation` et l'autorisation `access_as_user` de l'API MCP test. Activer les flux clients publics pour le test interactif par code d'appareil. |
| `ent-shareiarag-test-mcp-<suffix>` | Exposer `api://<mcp-client-id>/access_as_user`, jetons d'acces v2. Preautoriser l'application console test pour ce scope. Declarer `https://search.azure.com/user_impersonation` en permission deleguee pour l'echange OBO. |
| `ent-shareiarag-test-download-<suffix>` | Activer les revendications de groupes de securite (`groupMembershipClaims: SecurityGroup`) et **l'emission des jetons d'ID** (`web.implicitGrantSettings.enableIdTokenIssuance: true`) pour Easy Auth, sinon la connexion echoue avec `AADSTS700054`. Laisser l'emission implicite de jetons d'acces desactivee. Utiliser la plateforme Web. Son credential est cree plus tard, uniquement dans le Key Vault test. |

`create_entra_apps.py` automatise ces etapes ; chaque sous-commande affiche un apercu sans `--execute` et demande de saisir le nom du groupe de ressources avant toute modification :

- `python -B create_entra_apps.py create --execute` (avant le deploiement) : cree les trois inscriptions, leurs service principals, la preautorisation console -> MCP et les consentements administrateur (console -> Search et MCP, MCP -> Search), puis ecrit les trois ID client dans le fichier de parametres. Il refuse si le fichier contient deja des ID ou si un nom existe deja. L'application download est consentie par chaque utilisateur a sa premiere connexion (`openid profile email`).
- `python -B create_entra_apps.py post-deploy --execute` (apres le deploiement) : lit les hotes reels dans ARM, ajoute le redirect SPA de la console et le redirect Web de la Function, et cree le credential federe du MCP pour l'identite `id-shareiarag-test-mcp-<suffix>`.
- `python -B create_entra_apps.py sso --app-id-uri <uri> --execute` (optionnel) : voir la section Microsoft 365 Copilot.

## Deploiement : apres approbation uniquement

1. Copier `runtime.parameters.example.json` vers `runtime.parameters.json` (ignore par Git) et remplacer tous les placeholders : `suffix`, `resourceGroupName`, deployeur, administrateurs console, deux testeurs (A puis B), deux groupes (A puis B). Laisser vides les trois ID client (remplis par `create_entra_apps.py create`) et `mcpSsoAudience`. Aucun secret dans ce fichier.
2. What-if au niveau abonnement, puis deploiement avec les memes parametres :

   ```powershell
   cd infra\test
   $p = (Get-Content runtime.parameters.json -Raw | ConvertFrom-Json).parameters
   $name = "shareiarag-test-$($p.suffix.value)"
   az deployment sub what-if -n $name -l $p.location.value -f main.bicep -p runtime.parameters.json
   az deployment sub create  -n $name -l $p.location.value -f main.bicep -p runtime.parameters.json
   ```

   Stopper si le what-if touche un groupe ou une ressource hors de `resourceGroupName`, supprime une ressource ou signale une erreur. Verifier les tags, identites, noms et hotes reels. Le Bicep attribue au deployeur `Foundry Project Manager` et aux deux testeurs `Foundry Agent Consumer` et `Reader` sur le projet test.
3. Lancer `create_entra_apps.py post-deploy --execute` : credential federe du MCP (issuer `https://login.microsoftonline.com/<tenant-id>/v2.0`, subject = output ARM `mcpFederatedIdentityPrincipalId`, audience `api://AzureADTokenExchange`) et redirects sur les hotes reels.
4. Apres propagation RBAC, lancer `python -B seed_test_secrets.py files --execute`, puis `python -B seed_test_secrets.py download --execute`. Les valeurs restent en memoire et ne sont ni passees en argument ni affichees. Le credential download expire apres 30 jours. En cas d'erreur, inspecter et reconcilier les objets indiques **avant** toute reprise.
5. Refaire what-if puis deploiement avec `-p grantSecretAccess=true` pour attribuer les droits sur les deux secrets. Attendre la propagation, puis forcer le rafraichissement des references Key Vault de la console et de la Function ([documentation](https://learn.microsoft.com/azure/app-service/app-service-key-vault-references)).
6. Verifier les redirects, les consentements et la federation. Ne pas desactiver l'authentification ni activer la publication Basic pour contourner une erreur.
7. Construire de nouveaux ZIP dans un repertoire hors des sources :

   ```powershell
   python -B infra\test\build_app_package.py console --output <chemin>\console.zip
   python -B infra\test\build_app_package.py mcp --output <chemin>\mcp.zip
   python -B infra\test\build_function_package.py --output <chemin>\download.zip
   ```

   Le paquet Function epingle le tenant courant et le `downloadClientId` du fichier de parametres dans sa copie, et refuse l'application de l'environnement initial. Ses fichiers portent les permissions POSIX `0644`, necessaires a l'hote Linux Flex (sinon HTTP 503) ; ne pas recreer ce ZIP avec un autre outil. Publier console et MCP via `az webapp deploy --type zip`, la Function via `az functionapp deployment source config-zip --build-remote true`. Verifier les builds distants et l'application reelle. Si `az webapp deploy` reste bloque, verifier l'etat avec `az webapp log deployment list` avant de relancer.
8. Charger uniquement `fixtures\admin-test.txt` dans `admin-share` et `fixtures\alluser-test.txt` dans `alluser-share` du compte `stshragfiles<suffix>`, avec `--auth-mode login --backup-intent`, sans cle en ligne de commande.
9. Previsualiser puis executer `python -B infra\test\bootstrap_search.py [--execute]`. Les groupes viennent du fichier de parametres. Les neuf objets doivent etre absents au depart. Verifier les deux indexeurs et l'indexation des deux fichiers.
10. Executer `python -B infra\test\bootstrap_foundry.py --execute`. Le script cree la connexion MCP Search avec l'audience `https://search.azure.com/` (sans elle, l'agent echoue avec `tool_user_error`), puis `agent-shareiarag-test`. Il refuse les objets deja presents.

## Connecteur Microsoft 365 Copilot (optionnel, seconde passe)

1. Dans le Teams Developer Portal, creer une inscription Entra SSO pour l'application MCP test (scope `api://<mcp-client-id>/access_as_user`, hote MCP reel). Noter son Application ID URI et son registration ID.
2. Lancer `create_entra_apps.py sso --app-id-uri <application-id-uri> --execute` : ajoute cet URI aux `identifierUris` de `ent-shareiarag-test-mcp-<suffix>`, le redirect Web `https://teams.microsoft.com/api/platform/v1.0/oAuthConsentRedirect`, preautorise le client Microsoft Enterprise Token Store `ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b` pour `access_as_user` et ecrit `mcpSsoAudience` dans le fichier de parametres.
3. Refaire what-if et deploiement. Le Bicep ajoute alors cette audience et le client Microsoft 365 aux parametres du MCP ; sans `mcpSsoAudience`, seule la console est autorisee.
4. Dans le centre d'administration Microsoft 365, creer le connecteur avec le registration ID comme Reference ID et limiter le rollout aux testeurs.

## Verification fonctionnelle

- Verifier `/healthz` pour les deux Web Apps, le deploiement de la Function, l'etat des references Key Vault et les deux indexeurs. Un endpoint sain ne prouve pas les autorisations.
- Lancer localement `python -B infra\test\verify_mcp_access.py` (ID et groupes lus dans le fichier de parametres). Il ouvre deux flux de connexion par code d'appareil distincts ; chaque personne saisit son code elle-meme. Ne pas partager les codes ni les jetons. Le test exige les resultats autorises, les refus croises `fetch` et les rejets sans jeton ou avec jeton invalide. Il accepte comme clients MCP autorises la console test, plus le client Microsoft 365 seulement si le SSO est active. Si la politique Entra bloque le flux, arreter et choisir une methode approuvee.

- Dans deux profils de navigateur independants, verifier que A ouvre `admin-share\admin-test.txt` et que B ouvre `alluser-share\alluser-test.txt`, puis verifier les deux acces croises refuses. Les redirections peuvent contenir un SAS temporaire : ne pas copier ou partager l'URL complete. En cas de depassement du nombre de groupes dans un jeton, la Function peut refuser l'acces ; ne pas interpreter ce refus comme une reussite fonctionnelle.
- Tester aussi l'agent Foundry avec les deux identites et les deux documents, en verifiant les sources et les refus croises. Le Playground du portail exige le role `Foundry User` sur le projet : l'attribuer temporairement aux testeurs, puis le retirer et verifier son absence ; `Foundry Agent Consumer` suffit pour un client qui invoque l'agent publie. Foundry documente la transmission du jeton utilisateur vers la knowledge base, mais cette integration et les consentements doivent etre verifies en conditions reelles : [delegation Foundry IQ](https://learn.microsoft.com/azure/foundry/agents/concepts/user-delegation#foundry-iq-source-authorization).
- Si le connecteur Microsoft 365 est configure, tester dans Copilot avec chaque testeur, dans une nouvelle conversation : son document est trouve avec le bon code et la source de la Function test ; le document de l'autre groupe n'est ni cite ni revele.
- Ne declarer le test reussi que si les trois applications, les deux indexeurs, le filtre Search, les telechargements et l'agent ont passe leurs verifications positives et negatives. Sinon signaler le composant en echec et conserver les preuves sans secrets.

## Nettoyage

La suppression du groupe test n'est jamais automatique et doit etre approuvee separement. Avant suppression, enregistrer les ID et verifier les couts en cours. Les trois inscriptions Entra, leurs consentements et les definitions de roles personnalises peuvent survivre au groupe : les inventorier et demander une approbation specifique avant leur suppression ciblee. Le Key Vault est en suppression recuperable pendant sept jours ; une purge est une action distincte. Ne jamais supprimer ou modifier les ressources, groupes ou inscriptions de l'environnement existant.
