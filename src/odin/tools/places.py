"""Места: результат геокодинга и редакционная таблица точек прибытия (план §2.5, Transfer).

- ``ResolvedPlace`` — город с часовым поясом IANA и координатами из источника (Open-Meteo в Live или
  демо-справочник в Mock). Совместим по атрибутам с ``DemoPlace``.
- ``TRANSIT_POINTS`` — запасная таблица вокзалов и аэропортов с **ориентировочными** координатами
  (редакционные данные, не источник истины): нужна только для оценки времени трансфера OSRM, потому что
  Tutu не отдаёт координаты станций. Оценка времени по ней — ``estimate_osrm`` и никогда не ``matched``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from odin.contracts import Place, SourceReference


@dataclass(frozen=True, slots=True)
class ResolvedPlace:
    name: str
    country_code: str | None
    timezone: str | None
    lat: float | None
    lon: float | None
    source: SourceReference | None = None
    aliases: tuple[str, ...] = field(default=())

    def contract(self) -> Place:
        return Place(
            name=self.name,
            country_code=self.country_code,
            timezone=self.timezone,
            lat=self.lat,
            lon=self.lon,
            source=self.source,
        )


@dataclass(frozen=True, slots=True)
class TransitPoint:
    key: str
    city: str
    kind: str  # rail | airport
    name: str
    lat: float
    lon: float
    patterns: tuple[str, ...]


TRANSIT_POINTS_LIMITATION = "координаты точки ориентировочные (редакционная таблица), не данные провайдера"

TRANSIT_POINTS: tuple[TransitPoint, ...] = (
    TransitPoint(
        "kzn-rail", "Казань", "rail", "вокзал Казань-Пассажирская", 55.7886, 49.0997, ("казань пасс",)
    ),
    TransitPoint("KZN", "Казань", "airport", "аэропорт Казань (KZN)", 55.6062, 49.2787, ("kzn",)),
    TransitPoint(
        "mow-kazansky", "Москва", "rail", "Казанский вокзал", 55.7738, 37.6566, ("казанский вокзал",)
    ),
    TransitPoint(
        "mow-leningradsky",
        "Москва",
        "rail",
        "Ленинградский вокзал",
        55.7764,
        37.6553,
        ("ленинградский вокзал",),
    ),
    TransitPoint(
        "mow-yaroslavsky", "Москва", "rail", "Ярославский вокзал", 55.7766, 37.6573, ("ярославский вокзал",)
    ),
    TransitPoint("mow-kursky", "Москва", "rail", "Курский вокзал", 55.7577, 37.6610, ("курский вокзал",)),
    TransitPoint(
        "mow-paveletsky", "Москва", "rail", "Павелецкий вокзал", 55.7297, 37.6400, ("павелецкий вокзал",)
    ),
    TransitPoint("mow-kievsky", "Москва", "rail", "Киевский вокзал", 55.7434, 37.5664, ("киевский вокзал",)),
    TransitPoint(
        "mow-belorussky", "Москва", "rail", "Белорусский вокзал", 55.7766, 37.5817, ("белорусский вокзал",)
    ),
    TransitPoint("mow-rizhsky", "Москва", "rail", "Рижский вокзал", 55.7930, 37.6324, ("рижский вокзал",)),
    TransitPoint(
        "mow-savyolovsky", "Москва", "rail", "Савёловский вокзал", 55.7942, 37.5880, ("савеловский вокзал",)
    ),
    TransitPoint("SVO", "Москва", "airport", "аэропорт Шереметьево (SVO)", 55.9726, 37.4146, ("svo",)),
    TransitPoint("DME", "Москва", "airport", "аэропорт Домодедово (DME)", 55.4088, 37.9063, ("dme",)),
    TransitPoint("VKO", "Москва", "airport", "аэропорт Внуково (VKO)", 55.5915, 37.2615, ("vko",)),
    TransitPoint("ZIA", "Москва", "airport", "аэропорт Жуковский (ZIA)", 55.5533, 38.1517, ("zia",)),
    TransitPoint(
        "led-moskovsky",
        "Санкт-Петербург",
        "rail",
        "Московский вокзал",
        59.9298,
        30.3624,
        ("санкт-петербург-главн", "московский вокзал"),
    ),
    TransitPoint(
        "led-ladozhsky", "Санкт-Петербург", "rail", "Ладожский вокзал", 59.9325, 30.4413, ("ладожский",)
    ),
    TransitPoint(
        "led-finlyandsky", "Санкт-Петербург", "rail", "Финляндский вокзал", 59.9557, 30.3561, ("финляндский",)
    ),
    TransitPoint(
        "led-vitebsky", "Санкт-Петербург", "rail", "Витебский вокзал", 59.9200, 30.3290, ("витебский",)
    ),
    TransitPoint("LED", "Санкт-Петербург", "airport", "аэропорт Пулково (LED)", 59.8003, 30.2625, ("led",)),
    TransitPoint("aer-sochi", "Сочи", "rail", "вокзал Сочи", 43.5917, 39.7275, ("сочи (",)),
    TransitPoint("aer-adler", "Сочи", "rail", "вокзал Адлер", 43.4302, 39.9213, ("адлер",)),
    TransitPoint("AER", "Сочи", "airport", "аэропорт Сочи (AER)", 43.4499, 39.9566, ("aer",)),
    TransitPoint(
        "goj-moskovsky",
        "Нижний Новгород",
        "rail",
        "Московский вокзал",
        56.3216,
        43.9457,
        ("нижний новгород",),
    ),
    TransitPoint("GOJ", "Нижний Новгород", "airport", "аэропорт Стригино (GOJ)", 56.2301, 43.7840, ("goj",)),
    TransitPoint("kgd-yuzhny", "Калининград", "rail", "Южный вокзал", 54.6926, 20.4997, ("калининград",)),
    TransitPoint("KGD", "Калининград", "airport", "аэропорт Храброво (KGD)", 54.8900, 20.5926, ("kgd",)),
    TransitPoint(
        "svx-rail", "Екатеринбург", "rail", "вокзал Екатеринбург-Пасс.", 56.8580, 60.6006, ("екатеринбург",)
    ),
    TransitPoint("SVX", "Екатеринбург", "airport", "аэропорт Кольцово (SVX)", 56.7431, 60.8027, ("svx",)),
    TransitPoint(
        "ovb-rail", "Новосибирск", "rail", "вокзал Новосибирск-Главный", 55.0353, 82.8975, ("новосибирск",)
    ),
    TransitPoint("OVB", "Новосибирск", "airport", "аэропорт Толмачёво (OVB)", 55.0126, 82.6507, ("ovb",)),
)

_IATA = re.compile(r"\b([A-Z]{3})\b")


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def match_transit_point(label: str | None, kind: str) -> TransitPoint | None:
    """Точка по строке провайдера: «Казань — Казань Пасс (2060500)», «Казань, KZN, терм. 1».

    Для аэропортов — только по коду IATA; для вокзалов — по фрагменту названия станции (после « — »).
    Совпадения нет — ``None`` (время трансфера останется неизвестным, а не выдуманным).
    """
    if not label:
        return None
    if kind == "airport":
        for code in _IATA.findall(label):
            for point in TRANSIT_POINTS:
                if point.kind == "airport" and point.key == code:
                    return point
        return None
    station = _norm(label.split("—", 1)[1] if "—" in label else label)
    for point in TRANSIT_POINTS:
        if point.kind == "rail" and any(pattern in station for pattern in point.patterns):
            return point
    return None
