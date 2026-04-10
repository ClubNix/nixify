# Nixify

Nixify est une application web locale pensée pour gérer la musique dans un espace partagé, comme un club, une salle associative ou un local équipé d’enceintes.

L’idée de départ était simple : éviter le bazar des téléphones branchés directement sur une enceinte, centraliser la lecture musicale sur une seule machine, et permettre à plusieurs personnes d’interagir facilement avec la bibliothèque depuis une interface web accessible sur le réseau local.

Le projet met à disposition une interface permettant d’ajouter des morceaux, de gérer une file d’attente, d’organiser la bibliothèque et de suivre la lecture en cours. En parallèle, un mode kiosque peut être affiché sur un écran dédié pour montrer en direct les informations utiles, comme le morceau en cours ou les indications de connexion.

## Objectif

Nixify a pour but de proposer une solution simple, autonome et pratique pour la gestion musicale collective en local.

Le projet répond à plusieurs besoins concrets :

- centraliser la lecture musicale sur une seule machine
- permettre à plusieurs utilisateurs d’ajouter leurs morceaux
- garder une file d’attente claire
- éviter les manipulations directes sur le PC relié aux enceintes
- afficher publiquement les informations de lecture sur un écran kiosque

## Technologies utilisées

Le projet repose sur une pile volontairement simple et adaptée à un usage local :

- **Python** pour l’application principale
- **Flask** pour le backend et la gestion des routes web
- **SQLite** pour le stockage des données
- **python-vlc** pour la lecture audio
- **HTML / CSS / JavaScript** pour l’interface utilisateur

## Fonctionnalités principales

Nixify a été développé progressivement, en ajoutant les fonctionnalités selon les besoins réels d’utilisation.

À ce jour, le projet permet notamment :

- la gestion des comptes utilisateurs
- l’upload de morceaux
- l’organisation de la bibliothèque musicale
- la mise en file d’attente des pistes
- la suppression et le renommage de morceaux
- la gestion des favoris et des playlists
- l’affichage des pochettes
- le contrôle du volume
- le filtrage des contenus par utilisateur
- un affichage kiosque dédié à un écran public

## Fonctionnement général

L’application tourne sur une machine connectée au réseau local et reliée au système audio.

Les utilisateurs accèdent à l’interface web depuis leur navigateur pour interagir avec la bibliothèque musicale. Le serveur Flask gère les actions côté application, SQLite stocke les informations importantes, et `python-vlc` pilote directement la lecture des pistes.

En complément, un mode kiosque peut être affiché sur un écran séparé afin de montrer l’état de la lecture en temps réel.

## Déploiement local

Le projet a été pensé pour fonctionner facilement dans un environnement local.

Il peut être lancé manuellement, mais aussi intégré à un démarrage automatique via un service `systemd`. Il est également possible de configurer un navigateur en mode kiosque pour afficher en permanence l’écran public.

## Structure du projet

La structure exacte peut évoluer, mais le projet s’organise globalement autour de :

- un backend Flask pour la logique de l’application
- une base SQLite pour les données
- des fichiers HTML, CSS et JavaScript pour l’interface
- un espace principal d’administration et de gestion
- un mode kiosque pour l’affichage public

## Installation

### Prérequis

Avant de lancer Nixify, il faut disposer de :

- Python 3
- `pip`
- VLC installé sur la machine
- les dépendances Python du projet

### Installation des dépendances

```bash
pip install -r requirements.txt
