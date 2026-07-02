#!/usr/bin/env bash
set -e

echo "=== Mise à jour du système ==="
sudo apt update
sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    build-essential \
    git

echo "=== Création du venv ==="
python3 -m venv venv

echo "=== Activation du venv ==="
source venv/bin/activate

echo "=== Mise à jour de pip ==="
pip install --upgrade pip setuptools wheel

echo "=== Installation des dépendances ==="
pip install \
    notebook \
    ipykernel \
    pandas \
    duckdb \
    dask[distributed,dataframe] \
    pyarrow \
    seaborn \
    matplotlib \
    s3fs \
    fastparquet

echo "=== Ajout du kernel Jupyter ==="
python -m ipykernel install --user --name=ssp-venv --display-name="Python (ssp-venv)"

echo "========================================"
echo "Installation terminée !"
echo ""
echo "Pour utiliser le venv :"
echo "source venv/bin/activate"
echo ""
echo "Pour lancer Jupyter :"
echo "jupyter notebook --no-browser --ip=0.0.0.0"
echo ""
echo "Puis ouvre le notebook via le tunnel SSH ou le proxy SSP Cloud."
echo "========================================"