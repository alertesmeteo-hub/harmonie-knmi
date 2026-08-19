import os
import requests
import pandas as pd
import folium
from folium.features import DivIcon

# 1. Récupération de la clé API
api_key = os.environ.get('METEOFRANCE_API_KEY')

if not api_key:
    print("Avertissement: La clé API Météo-France est introuvable.")

# 2. Configuration de la requête vers l'API v2
url = "https://public-api.meteofrance.fr/public/DPObs/v1/mesures"
headers = {'accept': 'application/json', 'apikey': api_key}
params = {'duree': '24', 'format': 'json'}

try:
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status() 
    df = pd.DataFrame(response.json())
except Exception as e:
    print(f"Erreur lors de l'appel API: {e}")
    df = pd.DataFrame()

# 3. Création de la carte Leaflet (Centrée sur la France avec zoom par défaut)
carte = folium.Map(location=[46.5, 2.5], zoom_start=6, tiles='OpenStreetMap')

# 4. Ajout des relevés sur la carte Leaflet
if not df.empty:
    for index, row in df.iterrows():
        try:
            pluie_24h = float(row.get('rr24', 0))
            lat = float(row['lat'])
            lon = float(row['lon'])
            
            # Ne garder que les stations avec au moins 0.2 mm
            if pluie_24h >= 0.2:
                # Couleurs
                if pluie_24h < 5: couleur = 'blue'
                elif pluie_24h < 15: couleur = '#008000' # Vert
                elif pluie_24h < 40: couleur = '#ff8800' # Orange
                elif pluie_24h < 70: couleur = 'red'
                else: couleur = 'purple'

                # Rendu du chiffre (façon Météo60) : contour blanc (text-shadow) pour la lisibilité
                html_label = f'<div style="font-size: 11px; font-weight: bold; color: {couleur}; text-shadow: 1px 1px 0 #fff, -1px 1px 0 #fff, 1px -1px 0 #fff, -1px -1px 0 #fff;">{round(pluie_24h, 1)}</div>'

                # Placement du chiffre sur la carte Leaflet sans marqueur standard
                folium.Marker(
                    location=[lat, lon],
                    icon=DivIcon(
                        icon_size=(30, 30),
                        icon_anchor=(15, 15),
                        html=html_label
                    )
                ).add_to(carte)
        except (ValueError, KeyError, TypeError):
            continue

# 5. Sauvegarde au format interactif HTML
carte.save('observations_pluie_24h.html')
print("Carte Leaflet générée avec succès : observations_pluie_24h.html")
