import requests
import folium
from folium.features import DivIcon

# URL de l'API publique Opendatasoft (Données officielles Météo-France SYNOP)
url = "https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets/donnees-synoptiques-courantes-temps-reel-synop/exports/json"
params = {
    "select": "nom,coordonnees,rr24",
    "limit": 150,
    "order_by": "date DESC"
}

try:
    response = requests.get(url, params=params)
    response.raise_for_status() 
    donnees = response.json()
    
    # Sécurisation du format de réponse JSON
    if isinstance(donnees, dict):
        if 'results' in donnees:
            donnees = donnees['results']
        elif 'records' in donnees:
            donnees = donnees['records']
            
except Exception as e:
    print(f"Erreur lors de l'appel API: {e}")
    donnees = []

# Création de la carte Leaflet
carte = folium.Map(location=[46.5, 2.5], zoom_start=6, tiles='OpenStreetMap')

if isinstance(donnees, list) and len(donnees) > 0:
    for obs in donnees:
        try:
            pluie_24h = obs.get('rr24')
            coords = obs.get('coordonnees')
            
            # On ne garde que les stations avec des coordonnées et au moins 0.2 mm
            if pluie_24h is not None and coords:
                pluie_24h = float(pluie_24h)
                
                if pluie_24h >= 0.2:
                    if isinstance(coords, dict):
                        lat = coords.get('lat')
                        lon = coords.get('lon')
                    elif isinstance(coords, list):
                        lat = coords[0]
                        lon = coords[1]
                    else:
                        continue
                        
                    if lat and lon:
                        # Rendu des couleurs
                        if pluie_24h < 5: couleur = 'blue'
                        elif pluie_24h < 15: couleur = '#008000'
                        elif pluie_24h < 40: couleur = '#ff8800'
                        elif pluie_24h < 70: couleur = 'red'
                        else: couleur = 'purple'

                        # Contour blanc pour la lisibilité
                        html_label = f'<div style="font-size: 11px; font-weight: bold; color: {couleur}; text-shadow: 1px 1px 0 #fff, -1px 1px 0 #fff, 1px -1px 0 #fff, -1px -1px 0 #fff;">{round(pluie_24h, 1)}</div>'

                        folium.Marker(
                            location=[lat, lon],
                            icon=DivIcon(
                                icon_size=(30, 30),
                                icon_anchor=(15, 15),
                                html=html_label
                            )
                        ).add_to(carte)
        except Exception:
            continue

carte.save('observations_pluie_24h.html')
print("Carte Leaflet générée avec succès via Opendatasoft !")
