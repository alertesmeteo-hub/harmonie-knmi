import os
import requests
import pandas as pd
import folium
from folium.features import DivIcon

api_key = os.environ.get('METEOFRANCE_API_KEY')

url = "https://public-api.meteofrance.fr/public/DPObs/v1/mesures"
headers = {'accept': 'application/json', 'apikey': api_key}
params = {'duree': '24', 'format': 'json'}

try:
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status() 
    df = pd.DataFrame(response.json())
except Exception as e:
    print(f"Erreur: {e}")
    df = pd.DataFrame()

carte = folium.Map(location=[46.5, 2.5], zoom_start=6, tiles='OpenStreetMap')

if not df.empty:
    for index, row in df.iterrows():
        try:
            pluie_24h = float(row.get('rr24', 0))
            lat = float(row['lat'])
            lon = float(row['lon'])
            
            if pluie_24h >= 0.2:
                if pluie_24h < 5: couleur = 'blue'
                elif pluie_24h < 15: couleur = '#008000'
                elif pluie_24h < 40: couleur = '#ff8800'
                elif pluie_24h < 70: couleur = 'red'
                else: couleur = 'purple'

                html_label = f'<div style="font-size: 11px; font-weight: bold; color: {couleur}; text-shadow: 1px 1px 0 #fff, -1px 1px 0 #fff, 1px -1px 0 #fff, -1px -1px 0 #fff;">{round(pluie_24h, 1)}</div>'

                folium.Marker(
                    location=[lat, lon],
                    icon=DivIcon(
                        icon_size=(30, 30),
                        icon_anchor=(15, 15),
                        html=html_label
                    )
                ).add_to(carte)
        except:
            continue

carte.save('observations_pluie_24h.html')
