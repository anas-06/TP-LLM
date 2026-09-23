"""Schéma des relations (Re-DocRED / Wikidata) et normalisation des libellés.

Re-DocRED annote 96 relations Wikidata (codes P...). Pour un modèle de 1B,
présenter les 96 relations dans le prompt est trop lourd : on travaille par
défaut sur un sous-ensemble « core » (CORE_RELATIONS), configurable.
"""

from __future__ import annotations

import re

# code Wikidata -> (libellé snake_case, définition courte utilisée dans le prompt)
RELATIONS: dict[str, tuple[str, str]] = {
    "P6": ("head_of_government", "subject (place) is governed by object (person)"),
    "P17": ("country", "subject is located in / belongs to the country object"),
    "P19": ("place_of_birth", "subject (person) was born in object (place)"),
    "P20": ("place_of_death", "subject (person) died in object (place)"),
    "P22": ("father", "object is the father of subject"),
    "P25": ("mother", "object is the mother of subject"),
    "P26": ("spouse", "subject is married to object"),
    "P27": ("country_of_citizenship", "subject (person) is a citizen of country object"),
    "P30": ("continent", "subject is located on continent object"),
    "P31": ("instance_of", "subject is an instance of class object"),
    "P35": ("head_of_state", "object is the head of state of subject (country)"),
    "P36": ("capital", "object is the capital of subject"),
    "P37": ("official_language", "object is an official language of subject"),
    "P39": ("position_held", "subject (person) holds or held position object"),
    "P40": ("child", "object is a child of subject"),
    "P50": ("author", "object is the author of subject (work)"),
    "P54": ("member_of_sports_team", "subject (person) plays or played for team object"),
    "P57": ("director", "object directed subject (film, show)"),
    "P58": ("screenwriter", "object wrote the screenplay of subject"),
    "P69": ("educated_at", "subject (person) studied at object (school, university)"),
    "P86": ("composer", "object composed subject (music)"),
    "P102": ("member_of_political_party", "subject (person) belongs to party object"),
    "P108": ("employer", "subject (person) works or worked for object (organization)"),
    "P112": ("founded_by", "subject (organization) was founded by object"),
    "P118": ("league", "subject (team, athlete) plays in league object"),
    "P123": ("publisher", "object published subject"),
    "P127": ("owned_by", "subject is owned by object"),
    "P131": ("located_in", "subject is located in the administrative area object (city, state, region)"),
    "P136": ("genre", "object is the genre of subject"),
    "P137": ("operator", "object operates subject"),
    "P140": ("religion", "object is the religion of subject"),
    "P150": ("contains_administrative_entity", "subject (area) contains the administrative area object"),
    "P155": ("follows", "subject comes after object in a series"),
    "P156": ("followed_by", "subject is followed by object in a series"),
    "P159": ("headquarters_location", "subject (organization) has its headquarters in object"),
    "P161": ("cast_member", "object is an actor in subject (film, show)"),
    "P162": ("producer", "object produced subject"),
    "P166": ("award_received", "subject received award object"),
    "P170": ("creator", "object created subject"),
    "P171": ("parent_taxon", "object is the parent taxon of subject"),
    "P172": ("ethnic_group", "subject belongs to ethnic group object"),
    "P175": ("performer", "object performs subject (song, album, tour)"),
    "P176": ("manufacturer", "object manufactures subject"),
    "P178": ("developer", "object developed subject (software, game)"),
    "P179": ("series", "subject is part of the series object"),
    "P190": ("sister_city", "subject and object are twin cities"),
    "P194": ("legislative_body", "object is the legislative body of subject"),
    "P205": ("basin_country", "the basin of subject (lake, river) is in country object"),
    "P206": ("located_next_to_body_of_water", "subject is located in or next to body of water object"),
    "P241": ("military_branch", "subject serves in military branch object"),
    "P264": ("record_label", "subject (artist, album) is on record label object"),
    "P272": ("production_company", "object is the production company of subject"),
    "P276": ("location", "subject takes place / is located at object"),
    "P279": ("subclass_of", "subject is a subclass of object"),
    "P355": ("subsidiary", "object is a subsidiary of subject"),
    "P361": ("part_of", "subject is a part of object"),
    "P364": ("original_language", "object is the original language of subject (work)"),
    "P400": ("platform", "subject (software, game) runs on platform object"),
    "P403": ("mouth_of_the_watercourse", "subject (river) flows into object"),
    "P449": ("original_network", "subject (show) was first broadcast on object"),
    "P463": ("member_of", "subject is a member of organization or group object"),
    "P488": ("chairperson", "object is the chairperson of subject"),
    "P495": ("country_of_origin", "subject (work, product) comes from country object"),
    "P527": ("has_part", "object is a part of subject"),
    "P551": ("residence", "subject (person) lives or lived in object"),
    "P569": ("date_of_birth", "subject (person) was born on date object"),
    "P570": ("date_of_death", "subject (person) died on date object"),
    "P571": ("inception", "subject was founded or created on date object"),
    "P576": ("dissolved", "subject was dissolved or demolished on date object"),
    "P577": ("publication_date", "subject (work) was published or released on date object"),
    "P580": ("start_time", "subject started on date object"),
    "P582": ("end_time", "subject ended on date object"),
    "P585": ("point_in_time", "subject happened on date object"),
    "P607": ("conflict", "subject took part in the conflict / war object"),
    "P674": ("characters", "object is a character in subject (work)"),
    "P676": ("lyrics_by", "object wrote the lyrics of subject"),
    "P706": ("located_on_terrain_feature", "subject is located on terrain feature object"),
    "P710": ("participant", "object participated in subject (event)"),
    "P737": ("influenced_by", "subject was influenced by object"),
    "P740": ("location_of_formation", "subject (group, organization) was formed in object"),
    "P749": ("parent_organization", "object is the parent organization of subject"),
    "P800": ("notable_work", "object is a notable work of subject"),
    "P807": ("separated_from", "subject was separated from object"),
    "P840": ("narrative_location", "the story of subject (work) takes place in object"),
    "P937": ("work_location", "subject (person) worked in place object"),
    "P1001": ("applies_to_jurisdiction", "subject (law, office) applies to jurisdiction object"),
    "P1056": ("product_produced", "subject produces product object"),
    "P1198": ("unemployment_rate", "object is the unemployment rate of subject"),
    "P1336": ("territory_claimed_by", "subject (territory) is claimed by object"),
    "P1344": ("participant_of", "subject participated in event object"),
    "P1365": ("replaces", "subject replaced object"),
    "P1366": ("replaced_by", "subject was replaced by object"),
    "P1376": ("capital_of", "subject is the capital of object"),
    "P1412": ("languages_spoken", "subject (person) speaks or writes language object"),
    "P1441": ("present_in_work", "subject (character) appears in work object"),
    "P3373": ("sibling", "subject and object are brothers or sisters"),
}

# Sous-ensemble par défaut : relations fréquentes dans Re-DocRED et
# compréhensibles par un petit modèle (personnes, organisations, lieux, dates).
CORE_RELATIONS: list[str] = [
    "P17", "P131", "P27", "P569", "P570", "P19", "P20", "P69", "P108",
    "P463", "P54", "P102", "P26", "P40", "P22", "P3373", "P571", "P577",
    "P175", "P50", "P57", "P159", "P112", "P264", "P30",
]

LABEL_TO_CODE: dict[str, str] = {label: code for code, (label, _) in RELATIONS.items()}

# Synonymes fréquemment produits par les modèles (surtout en zero-shot).
# Ils sont appliqués de la même façon à toutes les stratégies.
ALIASES: dict[str, str] = {
    "employed_by": "employer", "works_for": "employer", "worked_for": "employer",
    "works_at": "employer", "worked_at": "employer", "employee_of": "employer",
    "joined": "employer",
    "studied_at": "educated_at", "graduated_from": "educated_at", "attended": "educated_at",
    "alma_mater": "educated_at", "education": "educated_at",
    "born_in": "place_of_birth", "birthplace": "place_of_birth", "birth_place": "place_of_birth",
    "died_in": "place_of_death", "death_place": "place_of_death",
    "born_on": "date_of_birth", "birth_date": "date_of_birth", "birthdate": "date_of_birth",
    "died_on": "date_of_death", "death_date": "date_of_death",
    "nationality": "country_of_citizenship", "citizenship": "country_of_citizenship",
    "citizen_of": "country_of_citizenship",
    "married_to": "spouse", "wife": "spouse", "husband": "spouse", "wife_of": "spouse",
    "husband_of": "spouse",
    "has_child": "child", "son": "child", "daughter": "child",
    "brother": "sibling", "sister": "sibling", "sibling_of": "sibling",
    "located_in_the_administrative_territorial_entity": "located_in",
    "is_located_in": "located_in",
    "in_country": "country", "located_in_country": "country",
    "part_of_continent": "continent", "located_on_continent": "continent",
    "founded_in": "inception", "founded_on": "inception", "founding_date": "inception",
    "established": "inception", "established_in": "inception", "formed_in": "inception",
    "released_in": "publication_date", "release_date": "publication_date",
    "released_on": "publication_date", "published_in": "publication_date",
    "published_on": "publication_date",
    "performed_by": "performer", "artist": "performer", "singer": "performer",
    "written_by": "author", "author_of": "author", "wrote": "author",
    "directed_by": "director",
    "headquartered_in": "headquarters_location", "headquarters": "headquarters_location",
    "based_in": "headquarters_location",
    "founder": "founded_by",
    "label": "record_label", "signed_to": "record_label", "signed_with": "record_label",
    "member": "member_of", "belongs_to": "member_of",
    "plays_for": "member_of_sports_team", "played_for": "member_of_sports_team",
    "party": "member_of_political_party", "member_of_party": "member_of_political_party",
}


def canonical_label(raw: str) -> str:
    """Normalise un libellé de relation produit par le modèle vers le schéma."""
    label = raw.strip().lower()
    label = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
    if label.upper() in RELATIONS:  # le modèle a répondu avec un code "P69"
        return RELATIONS[label.upper()][0]
    return ALIASES.get(label, label)


def resolve_relation_set(spec: str | list[str] | None) -> list[str]:
    """'core' | 'all' | liste de codes/libellés -> liste de codes Wikidata."""
    if spec is None or spec == "core":
        return list(CORE_RELATIONS)
    if spec == "all":
        return list(RELATIONS)
    items = spec.split(",") if isinstance(spec, str) else spec
    codes = []
    for item in items:
        item = item.strip()
        if item.upper() in RELATIONS:
            codes.append(item.upper())
        elif canonical_label(item) in LABEL_TO_CODE:
            codes.append(LABEL_TO_CODE[canonical_label(item)])
        else:
            raise ValueError(f"Relation inconnue : {item}")
    return codes


def labels_for(codes: list[str]) -> list[str]:
    return [RELATIONS[c][0] for c in codes]
