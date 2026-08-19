import os
import requests
import pandas as pd
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature

# 1. Récupération de la clé API depuis les secrets GitHub
api_key = os.environ.get('METEOFRANCE_API_KEY')

if not api_key:
    print("Avertissement: La clé API Météo-France est introuvable. Assurez-vous de l'avoir configurée dans les variables d'environnement.")
    # Pour tester en local sur votre ordinateur, décommentez la ligne ci-dessous et insérez votre clé :
    # api_key = "VOTRE_CLE_API"

# 2. Configuration de la requête vers l'API v2
url = "https://public-api.meteofrance.fr/public/DPObs/v1/mesures"

headers = {
    'accept': 'application/json',
    'apikey': api_key  
}

# Paramètres de la requête (sur la France métropolitaine, dernières 24h)
params = {
    'duree': '24',
    'format': 'json'
}

# 3. Exécution de la requête
try:
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status() 
    donnees = response.json()
    df = pd.DataFrame(donnees)
except Exception as e:
    print(f"Erreur lors de l'appel API: {e}")
    df = pd.DataFrame()

# 4. Génération de la carte
fig = plt.figure(figsize=(12, 10))
ax = plt.axes(projection=ccrs.Mercator())

# Cadrage sur la France
ax.set_extent([-5.5, 10, 41, 52], crs=ccrs.PlateCarree())
ax.add_feature(cfeature.COASTLINE, linewidth=0.8, edgecolor='black')
ax.add_feature(cfeature.BORDERS, linestyle=':', linewidth=0.5)
ax.add_feature(cfeature.LAND, facecolor='#f4f4f4')
ax.add_feature(cfeature.OCEAN, facecolor='#e0f3ff')

# 5. Ajout des relevés sur la carte
if not df.empty:
    for index, row in df.iterrows():
        try:
            pluie_24h = float(row.get('rr24', 0))
            lat = float(row['lat'])
            lon = float(row['lon'])
            
            # Ne garder que les stations avec au moins 0.2 mm
            if pluie_24h >= 0.2:
                if pluie_24h < 5:
                    couleur = 'blue'
                elif pluie_24h < 15:
                    couleur = '#008000' # Vert foncé
                elif pluie_24h < 40:
                    couleur = '#ff8800' # Orange
                elif pluie_24h < 70:
                    couleur = 'red'
                else:
                    couleur = 'purple'

                ax.text(lon, lat, str(round(pluie_24h, 1)), 
                        transform=ccrs.PlateCarree(),
                        color=couleur, 
                        fontsize=9, 
                        fontweight='bold',
                        ha='center', va='center')
        except (ValueError, KeyError, TypeError):
            continue

plt.title('Précipitations observées sur 24h (Réseau Météo-France)', fontsize=14, pad=15)

# 6. Sauvegarde de l'image
plt.savefig('observations_pluie_24h.png', bbox_inches='tight', dpi=150)
print("Carte générée avec succès : observations_pluie_24h.png")
plt.close()
