# Historique des prix – Plein moins cher

Ce dépôt prépare une base historique à partir des flux quotidiens officiels du prix des carburants.

- Source : https://donnees.roulez-eco.fr/opendata/jour/AAAAMMJJ
- Documentation : https://www.prix-carburants.gouv.fr/rubrique/opendata/
- Le flux quotidien officiel ne permet de récupérer que les 30 derniers jours. Le traitement initialise donc la base avec ces 30 journées, puis l'enrichit chaque jour.
- Les données sont conservées par station et carburant dans 64 fragments JSON. Les versions compressées sont publiées sur GitHub Pages.

Les relevés représentent le prix observé dans chaque instantané quotidien. Ils ne reconstituent pas les changements intrajournaliers qui ne figurent pas dans l'instantané.
