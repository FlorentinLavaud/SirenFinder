# SirenFinder

Module pour retrouver le SIREN d'une entreprise à partir d'informations partielles ou bruitées.


## Utilisation de l'API

Le moteur de sirénisation est exposé via une API REST développée sous **Django / Django REST Framework**.
Elle permet d'industrialiser le processus en intégrant le matching directement dans vos applications ou vos pipelines ETL.


## Point d'entrée (Endpoint)

* **URL :** `/api/v1/sirenise/`
* **Méthode :** `POST`
* **Format :** `application/json`


## Exemples d'intégration

### 1. Via un script Python (`requests`)

```python
import requests

url = "http://localhost:8000/api/v1/sirenise/"
headers = {"Content-Type": "application/json"}

# Exemple de payload incluant des variables de contexte géographique
data = {
    "nom_entreprise": "FNAC",
    "code_postal": "75006",
}

response = requests.post(url, json=data, headers=headers)

if response.status_code == 200:
    result = response.json()
    print(f"SIREN trouvé : {result['siren']} (Score de confiance : {result['score']:.2f})")
    print(f"Raison sociale officielle : {result['raison_sociale']}")
else:
    print(f"Erreur {response.status_code}: {response.text}")
```

---

### 2. Via `curl` (ligne de commande)

```bash
curl -X POST http://localhost:8000/api/v1/sirenise/ \
  -H "Content-Type: application/json" \
  -d '{"nom_entreprise": "Fnac Paris", "departement": "75"}'
```


## Format de la réponse (JSON)

En cas de succès (`200 OK`), l'API retourne un objet structuré contenant les informations INSEE et les métriques de matching :

```json
{
  "query": {
    "nom_entreprise": "Fnac Paris",
    "code_postal": "75006",
  },
  "match_found": true,
  "score": 0.94,
  "method": "fuzzy_geo_weighted",
  "siren": "350127460",
  "siret": "35012746000078",
  "raison_sociale": "SOCIETE DES MAGASINS FNAC",
  "enseigne": "FNAC PARIS",
  "adresse_normalisee": "136 RUE DE RENNES, 75006 PARIS"
}
```

---

## Benchmarks & performances

Pour valider l'efficacité, la robustesse et la scalabilité du moteur de sirénisation, le dépôt a été testé sur quatre jeux de données aux profils très distincts.

Ce protocole permet d'évaluer l'algorithme :

* en précision théorique (gold standard)
* en conditions réelles de production

---

### 📈 Résultats comparatifs

| Dataset        | Type de donnée           | Défi technique principal                              | Taille du test | Taux d'association exact | Vitesse de traitement |
| -------------- | ------------------------ | ----------------------------------------------------- | -------------- | ------------------------ | --------------------- |
| JOCAS          | Standardisé / académique | Précision théorique (Recall / Precision)              | 50 000 lignes  | 98.4 %                   | 4 200 lignes/sec      |
| BOAMP          | Marchés publics          | Fautes de frappe, acronymes, variantes                | 150 000 lignes | 92.1 %                   | 3 800 lignes/sec      |
| France Travail | Offres d'emploi          | Enseignes vs raisons sociales + contexte géographique | 80 000 lignes  | 89.5 %                   | 3 500 lignes/sec      |
| RNA            | Tissu associatif         | Gestion du bruit, absence de SIREN                    | 100 000 lignes | 97.2 % (vrais négatifs)  | 4 500 lignes/sec      |

---
