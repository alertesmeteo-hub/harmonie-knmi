import requests
import folium
from folium.features import DivIcon

# URL de l'API Opendatasoft (Données SYNOP Météo-France)
url = "https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets/donnees-synoptiques-courantes-temps-reel-synop/exports/json"

# On remonte sur 30h pour être certain d'attraper le relevé quotidien rr24 (souvent publié à 06h00)
params = {
    "where": "date >= now(hours=-30)",
    "select": "numer_sta,coordonnees,rr24,date"
}

print("Récupération des données Météo-France...")
try:
    response = requests.get(url, params=params)
    response.raise_for_status()
    donnees = response.json()
except Exception as e:
    print(f"Erreur API: {e}")
    donnees = []

releves_pluie = {}

# Tri pour ne garder que la donnée la plus récente par station
for obs in donnees:
    sta = obs.get('numer_sta')
    rr24 = obs.get('rr24')
    coords = obs.get('coordonnees')
    date_obs = obs.get('date')
    
    if sta and coords and rr24 is not None:
        try:
            val = float(rr24)
            # On ne garde que s'il a plu (>= 0.2 mm)
            if val >= 0.2:
                # Si on n'a pas encore la station ou si la date étudiée est plus récente
                if sta not in releves_pluie or date_obs > releves_pluie[sta]['date']:
                    if isinstance(coords, dict):
                        lat, lon = coords.get('lat'), coords.get('lon')
                    elif isinstance(coords, list):
                        lat, lon = coords[0], coords[1]
                    else:
                        continue
                        
                    releves_pluie[sta] = {
                        'val': val,
                        'lat': lat,
                        'lon': lon,
                        'date': date_obs
                    }
        except Exception:
            continue

# Création de la carte interactive
carte = folium.Map(location=[46.5, 2.5], zoom_start=6, tiles='OpenStreetMap')

for sta, data in releves_pluie.items():
    pluie = data['val']
    
    # Couleurs dynamiques selon le cumul
    if pluie < 5: couleur = 'blue'
    elif pluie < 15: couleur = '#008000'
    elif pluie < 40: couleur = '#ff8800'
    elif pluie < 70: couleur = 'red'
    else: couleur = 'purple'

    html_label = f'<div style="font-size: 11px; font-weight: bold; color: {couleur}; text-shadow: 1px 1px 0 #fff, -1px 1px 0 #fff, 1px -1px 0 #fff, -1px -1px 0 #fff;">{round(pluie, 1)}</div>'

    folium.Marker(
        location=[data['lat'], data['lon']],
        icon=DivIcon(
            icon_size=(30, 30),
            icon_anchor=(15, 15),
            html=html_label
        )
    ).add_to(carte)

carte.save('observations_pluie_24h.html')
print(f"Terminé ! {len(releves_pluie)} stations affichées sur la carte.")
