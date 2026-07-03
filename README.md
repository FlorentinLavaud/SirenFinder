# SirenFinder
Module pour retrouver le SIREN d'une entreprise à partir d'information partielle.

## Structure

- `siren_resolver/` : package générique (cache parquet, providers, pipeline).
  Réutilisable pour n'importe quelle source (fichiers CONTRACTING/AWARDED
  d'origine, JOCAS, ou une future source).
- `jocas_common.py` : tout ce qui est spécifique à JOCAS et partagé entre
  les deux scripts ci-dessous (connexion S3, normalisation des colonnes
  numériques cassées, blacklist des noms placeholder).
- `run_jocas.py` : lance la résolution SIREN sur JOCAS.
- `diagnostics.py` : mesure la couverture SIREN de JOCAS (sans rien
  résoudre), pour savoir combien d'offres le pipeline peut réellement viser
  avant de lancer un run coûteux en appels API.

## Prérequis

```bash
pip install -r requirements.txt
export AWS_S3_ENDPOINT=...       # endpoint SSP Cloud/OVH
export GOOGLE_CSE_API_KEY=...    # optionnel, active le fallback Google CSE
export GOOGLE_CSE_ID=...
```

## Diagnostics (à lancer avant tout run)

```bash
python diagnostics.py
```

Affiche :
- **Couverture globale** : % d'offres sans nom d'entreprise (hors de
  portée), % sur des placeholders connus (Pôle emploi, "Confidentiel"...,
  hors scope volontairement), et % réellement ciblé par le pipeline.
- **Top entités sans SIREN** (avec et sans blacklist appliquée) : sert à
  repérer de nouveaux placeholders à ajouter dans
  `jocas_common.NAME_BLACKLIST` avant de lancer un run massif — une entité
  en tête de liste avec un volume disproportionné par rapport à une
  vraie entreprise est suspecte.
- **Courbe de rendement** : couverture obtenue selon le nombre minimal
  d'occurrences par entité, pour choisir `--min-offers`.

## Résolution

```bash
# aperçu du volume sans rien exécuter
python run_jocas.py --dry-run

# résolution complète
python run_jocas.py

# ne traiter que les entités récurrentes (meilleur ratio coût API / gain)
python run_jocas.py --min-offers 3

# test rapide sur un sous-ensemble
python run_jocas.py --limit 200 --dry-run
```

### Points d'attention gérés

- **Colonnes numériques "cassées"** : `location_zipcode`,
  `location_departement` et `entreprise_siren` peuvent être typées `DOUBLE`
  côté parquet (NaN mélangés à des valeurs), ce qui produit des suffixes
  `.0` une fois castées en texte (`"34130.0"` au lieu de `"34130"`) et casse
  le matching avec l'API officielle. Neutralisé systématiquement par
  `jocas_common.sql_clean_numeric` / `clean_numeric_str`, avec zero-padding
  du SIREN sur 9 chiffres (récupère les SIREN commençant par `0`).
- **Placeholders non-entreprises** (`jocas_common.NAME_BLACKLIST`) : des
  noms comme "Pôle emploi" ou "Confidentiel" désignent une offre anonymisée,
  pas une vraie entreprise. Les exclure évite de gaspiller des appels API et
  surtout d'attribuer un faux SIREN qui contaminerait potentiellement des
  centaines de milliers de lignes d'un coup (cas observé : "Pôle emploi"
  était l'entité la plus fréquente parmi les offres sans SIREN).
- **Colonnes techniques Dask** (`__index_level_0__`,
  `__null_dask_index__`) : injectées lors de l'écriture par certains
  fichiers du glob S3, exclues automatiquement de l'export final.
