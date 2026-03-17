"""
Single-file product parser for Brain.com.ua product pages.

Flow:
1. Download product page HTML once and save it locally.
2. Read product data from the local HTML cache.
3. Parse structured product information with BeautifulSoup.
4. Save parsed data into Django model.

This module intentionally keeps all logic in one file to satisfy project
requirements, while still following separation of responsibilities.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

import load_django
from parser_app.models import Product

PRODUCT_URL = (
    "https://brain.com.ua/ukr/"
    "Mobilniy_telefon_Apple_iPhone_16_Pro_Max_256GB_Black_Titanium-p1145443.html"
)

# Local cache path for the downloaded product page.
CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_FILE = CACHE_DIR / "brain_product_page.html"

DEFAULT_TEXT = "None"
DEFAULT_PRICE = Decimal("0.00")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class ProductData:
    """Structured DTO for parsed product data."""

    name: str = DEFAULT_TEXT
    color: str = DEFAULT_TEXT
    memory: str = DEFAULT_TEXT
    manufacturer: str = DEFAULT_TEXT
    price: Decimal = DEFAULT_PRICE
    price_discount: Decimal = DEFAULT_PRICE
    photos: list[str] = field(default_factory=list)
    goods_code: str = "UNKNOWN"
    reviews_count: int = 0
    screen_size: str = DEFAULT_TEXT
    screen_resolution: str = DEFAULT_TEXT
    characteristics: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert DTO into a serializable dictionary."""
        return asdict(self)


class HtmlUtils:
    """Utility helpers for safe HTML extraction and normalization."""

    @staticmethod
    def clean_text(value: Optional[str], default: str = DEFAULT_TEXT) -> str:
        """
        Normalize whitespace and HTML non-breaking spaces.
        Returns default when input is empty.
        """
        if not value:
            return default

        normalized = " ".join(value.replace("\xa0", " ").split())
        return normalized if normalized else default

    @staticmethod
    def get_text(element: Optional[Tag], default: str = DEFAULT_TEXT) -> str:
        """Safely extract normalized text from a BeautifulSoup element."""
        if element is None:
            return default
        return HtmlUtils.clean_text(element.get_text(strip=True), default=default)

    @staticmethod
    def get_attr(element: Optional[Tag], attr_name: str, default: str = "") -> str:
        """Safely extract an attribute from a BeautifulSoup element."""
        if element is None:
            return default

        value = element.get(attr_name)
        if not isinstance(value, str):
            return default

        return value.strip() if value.strip() else default

    @staticmethod
    def to_decimal(value: Optional[str], default: Decimal = DEFAULT_PRICE) -> Decimal:
        """
        Convert price-like string to Decimal.

        Example:
        '1 125' -> Decimal('1125')
        '1 125 ₴' -> Decimal('1125')
        """
        if not value:
            return default

        cleaned = (
            value.replace("₴", "")
            .replace("\xa0", "")
            .replace(" ", "")
            .replace(",", ".")
            .strip()
        )

        if not cleaned:
            return default

        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return default


class LocalCachedPageLoader:
    """
    Loads HTML from a local cache file.

    If cache file does not exist, downloads the page once and stores it locally.
    """

    def __init__(self, cache_file: Path) -> None:
        self.cache_file = cache_file
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    def load(self, url: str) -> str:
        """
        Return HTML from cache if available.

        Otherwise download it, persist locally, and return downloaded HTML.
        """
        if self.cache_file.exists():
            logger.info("Reading product page from local cache: %s", self.cache_file)
            return self.cache_file.read_text(encoding="utf-8")

        logger.info("Local cache not found. Downloading page: %s", url)
        html = self._download(url)
        self._save_cache(html)
        return html

    def _download(self, url: str) -> str:
        """Download page HTML from remote source."""
        response = requests.get(url, headers=self.headers, timeout=30)
        response.raise_for_status()
        return response.text

    def _save_cache(self, html: str) -> None:
        """Persist downloaded HTML into a local cache file."""
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(html, encoding="utf-8")
        logger.info("Saved local cache: %s", self.cache_file)


class BrainProductParser:
    """Parser for a single Brain.com.ua product page."""

    def parse(self, html: str) -> ProductData:
        """Parse full product data from HTML."""
        soup = BeautifulSoup(html, "html.parser")

        characteristics = self._parse_characteristics(soup)

        product_data = ProductData(
            name=self._parse_name(soup),
            color=characteristics.get("Колір", DEFAULT_TEXT),
            memory=characteristics.get("Вбудована пам'ять", DEFAULT_TEXT),
            manufacturer=characteristics.get("Виробник", DEFAULT_TEXT),
            price=self._parse_price(soup),
            price_discount=self._parse_price_discount(soup),
            photos=self._parse_photos(soup),
            goods_code=self._parse_goods_code(soup),
            reviews_count=self._parse_reviews_count(soup),
            screen_size=characteristics.get("Діагональ екрану", DEFAULT_TEXT),
            screen_resolution=characteristics.get(
                "Роздільна здатність екрану", DEFAULT_TEXT
            ),
            characteristics=characteristics,
        )

        return product_data

    @staticmethod
    def _parse_name(soup: BeautifulSoup) -> str:
        """
        Parse product full name.

        Selector provided by user:
        //*[@class="fnp-product-name"]
        """
        element = soup.select_one(".fnp-product-name")
        return HtmlUtils.get_text(element)

    @staticmethod
    def _parse_price(soup: BeautifulSoup) -> Decimal:
        """
        Parse regular price.

        Strategy:
        - search inside .main-right-block
        - prefer .br-pr-op .price-wrapper > span
        - if missing, fallback to DEFAULT_PRICE
        """
        container = soup.select_one(".main-right-block")
        if container is None:
            logger.warning("Regular price container '.main-right-block' not found.")
            return DEFAULT_PRICE

        price_element = container.select_one(".br-pr-op .price-wrapper > span")
        if price_element is None:
            logger.warning("Regular price element not found. Using default price.")
            return DEFAULT_PRICE

        price_text = HtmlUtils.get_text(price_element, default="0")
        return HtmlUtils.to_decimal(price_text, default=DEFAULT_PRICE)

    @staticmethod
    def _parse_price_discount(soup: BeautifulSoup) -> Decimal:
        """
        Parse discount price.

        Strategy:
        - search inside .main-right-block
        - parse .red-price
        - if missing, return 0.00
        """
        container = soup.select_one(".main-right-block")
        if container is None:
            logger.warning("Discount price container '.main-right-block' not found.")
            return DEFAULT_PRICE

        discount_element = container.select_one(".red-price")
        if discount_element is None:
            logger.info(
                "Discount price element not found. Product probably has no discount."
            )
            return DEFAULT_PRICE

        discount_text = HtmlUtils.get_text(discount_element, default="0")
        return HtmlUtils.to_decimal(discount_text, default=DEFAULT_PRICE)

    @staticmethod
    def _parse_goods_code(soup: BeautifulSoup) -> str:
        """
        Parse product code.

        Expected selector:
        div.product-code-num span.br-pr-code-val
        """
        element = soup.select_one("div.product-code-num span.br-pr-code-val")
        goods_code = HtmlUtils.get_text(element, default="UNKNOWN")
        if goods_code == "UNKNOWN":
            logger.warning("Product code not found.")
        return goods_code

    @staticmethod
    def _parse_reviews_count(soup: BeautifulSoup) -> int:
        """
        Parse reviews count from the reviews anchor.

        Strategy:
        - find all anchors with href="#reviews-list" and class "scroll-to-element"
        - select the first anchor that contains a nested span with a numeric value
        - return 0 if nothing valid is found
        """
        links = soup.select('a.scroll-to-element[href="#reviews-list"]')
        if not links:
            logger.info("Reviews links not found. Using 0.")
            return 0

        for link in links:
            count_element = link.select_one("span")
            if count_element is None:
                continue

            raw_value = HtmlUtils.get_text(count_element, default="").strip()
            if not raw_value:
                continue

            try:
                return int(raw_value)
            except ValueError:
                logger.warning(
                    "Found reviews span, but value '%s' is not an integer.",
                    raw_value,
                )

        logger.warning("No valid reviews count span found. Using 0.")
        return 0

    @staticmethod
    def _parse_photos(soup: BeautifulSoup) -> list[str]:
        """
        Parse all product photo URLs.

        Strategy based on provided structure:
        - search in .main-left-block .product-block-bottom
        - collect img[src]
        - deduplicate while preserving order
        """
        photos: list[str] = []

        container = soup.select_one(".main-left-block .product-block-bottom")
        if container is None:
            logger.warning("Photos container not found.")
            return photos

        for img in container.select("img[src]"):
            src = HtmlUtils.get_attr(img, "src")
            if not src:
                continue
            if src not in photos:
                photos.append(src)

        if not photos:
            logger.warning("No product photos found in expected gallery container.")

        return photos

    @staticmethod
    def _parse_characteristics(soup: BeautifulSoup) -> dict[str, str]:
        """
        Parse all product characteristics as a flat key-value dictionary.
        """
        characteristics: dict[str, str] = {}

        root = soup.select_one("div#br-pr-7.br-pr-tblock.br-pr-chr-wrap")
        if root is None:
            logger.warning("Characteristics root block '#br-pr-7' not found.")
            return characteristics

        items = root.select(".br-pr-chr-item")
        if not items:
            logger.warning("No characteristic groups found inside '#br-pr-7'.")
            return characteristics

        for item in items:
            rows = item.select(":scope > div > div")
            for row in rows:
                parsed_row = BrainProductParser._parse_characteristic_row(row)
                if parsed_row is None:
                    continue

                key, value = parsed_row
                characteristics[key] = value

        return characteristics

    @staticmethod
    def _parse_characteristic_row(row: Tag) -> tuple[str, str] | None:
        """
        Parse one characteristic row into a (key, value) tuple.

        Expected row structure:
        <div>
            <span>Key</span>
            <span>Value</span>
        </div>
        """
        span_elements = row.find_all("span", recursive=False)
        if len(span_elements) < 2:
            return None

        key_element = span_elements[0]
        value_element = span_elements[1]

        key = HtmlUtils.get_text(key_element, default="")
        value = HtmlUtils.get_text(value_element, default="")

        if not key:
            return None

        return key, value

class ProductSaver:
    """Persist parsed product data into the Django database."""

    @staticmethod
    def save(product_data: ProductData) -> Product:
        """
        Save product using update_or_create.

        Product code is used as the unique business key.
        """
        if product_data.goods_code == "UNKNOWN":
            raise ValueError("Cannot save product with UNKNOWN goods_code.")

        product, created = Product.objects.update_or_create(
            goods_code=product_data.goods_code,
            defaults={
                "name": product_data.name,
                "color": product_data.color,
                "memory": product_data.memory,
                "manufacturer": product_data.manufacturer,
                "price": product_data.price,
                "price_discount": product_data.price_discount,
                "photos": product_data.photos,
                "reviews_count": product_data.reviews_count,
                "screen_size": product_data.screen_size,
                "screen_resolution": product_data.screen_resolution,
                "characteristics": product_data.characteristics,
            },
        )

        logger.info(
            "%s product with goods_code=%s",
            "Created" if created else "Updated",
            product.goods_code,
        )
        return product


class ProductParseService:
    """
    Orchestrates the full parsing workflow.

    Responsibilities:
    - load HTML from local cache or download once
    - parse structured data
    - save to database
    """

    def __init__(
        self,
        loader: LocalCachedPageLoader,
        parser: BrainProductParser,
        saver: ProductSaver,
    ) -> None:
        self.loader = loader
        self.parser = parser
        self.saver = saver

    def execute(self, url: str) -> ProductData:
        """Run full parse-and-save workflow."""
        html = self.loader.load(url)
        product_data = self.parser.parse(html)
        self.saver.save(product_data)
        return product_data


def main() -> None:
    """Application entry point."""
    service = ProductParseService(
        loader=LocalCachedPageLoader(cache_file=CACHE_FILE),
        parser=BrainProductParser(),
        saver=ProductSaver(),
    )

    try:
        product_data = service.execute(PRODUCT_URL)
    except requests.RequestException as exc:
        logger.exception("Network error while downloading product page: %s", exc)
        raise
    except Exception as exc:
        logger.exception("Unexpected error during parsing workflow: %s", exc)
        raise

    print(json.dumps(product_data.to_dict(), ensure_ascii=False, indent=4, default=str))


if __name__ == "__main__":
    main()
