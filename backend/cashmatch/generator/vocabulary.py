"""Word lists for a fictional Indian FMCG distributor's receivables book.

Faker's generic company names ("Smith-Jones LLC") would make the payer-name
matching problem unrealistically easy, because the hard part is specifically
Indian trade-name conventions: honorific prefixes, deity and surname stems,
trade suffixes, and the handful of legal forms that bank systems truncate
differently. These lists are built to produce that texture.

Everything here is a plain tuple so iteration order is fixed and generation
stays reproducible from the seed.
"""

from __future__ import annotations

# Honorific or qualifier that often opens an Indian trade name.
NAME_PREFIXES: tuple[str, ...] = (
    "Shree",
    "Sri",
    "Shri",
    "New",
    "Jai",
    "Om",
    "Maa",
    "Guru",
)

# Deity, place and surname stems that form the distinctive core of the name.
NAME_STEMS: tuple[str, ...] = (
    "Balaji",
    "Venkateshwara",
    "Annapurna",
    "Krishna",
    "Ganesh",
    "Laxmi",
    "Saraswati",
    "Maruti",
    "Sai",
    "Shakti",
    "Surya",
    "Gokul",
    "Narmada",
    "Kaveri",
    "Godavari",
    "Himalaya",
    "Konark",
    "Ajanta",
    "Deccan",
    "Malabar",
    "Agarwal",
    "Gupta",
    "Sharma",
    "Patel",
    "Reddy",
    "Nair",
    "Iyer",
    "Mehta",
    "Joshi",
    "Desai",
    "Shah",
    "Verma",
    "Rao",
    "Pillai",
    "Chatterjee",
    "Banerjee",
    "Kulkarni",
    "Deshmukh",
    "Chawla",
    "Bhatia",
    "Sethi",
    "Kapoor",
    "Menon",
    "Thakur",
    "Saxena",
    "Mishra",
    "Prasad",
    "Jain",
    "Bose",
    "Dutta",
)

# Trade suffixes. These are what a bank's narration field tends to mangle.
NAME_SUFFIXES: tuple[str, ...] = (
    "Traders",
    "Distributors",
    "Agencies",
    "Enterprises",
    "Trading Company",
    "Sales Corporation",
    "Marketing",
    "Stores",
    "& Sons",
    "Sales",
)

# Legal forms. Normalisation strips these, which is why two customers can
# differ only by legal form and still have to be told apart.
LEGAL_FORMS: tuple[str, ...] = (
    "Pvt Ltd",
    "Private Limited",
    "",
    "",
    "LLP",
)

CITIES: tuple[str, ...] = (
    "Mumbai",
    "Pune",
    "Nagpur",
    "Nashik",
    "Ahmedabad",
    "Surat",
    "Rajkot",
    "Indore",
    "Bhopal",
    "Jaipur",
    "Jodhpur",
    "Lucknow",
    "Kanpur",
    "Varanasi",
    "Patna",
    "Ranchi",
    "Kolkata",
    "Siliguri",
    "Guwahati",
    "Bhubaneswar",
    "Hyderabad",
    "Vijayawada",
    "Visakhapatnam",
    "Bengaluru",
    "Mysuru",
    "Hubballi",
    "Chennai",
    "Coimbatore",
    "Madurai",
    "Kochi",
    "Thrissur",
    "Ludhiana",
    "Amritsar",
    "Chandigarh",
    "Dehradun",
    "Delhi",
    "Faridabad",
)

# Our own collecting accounts, for the bank_account column.
COLLECTING_ACCOUNTS: tuple[str, ...] = (
    "HDFC-50200012345678",
    "ICIC-003405001234",
    "SBIN-39012345678",
)

# FMCG categories, used in remittance prose and deduction notes so the text
# reads like a real distributor's correspondence.
PRODUCT_CATEGORIES: tuple[str, ...] = (
    "biscuits",
    "detergent",
    "hair oil",
    "tea",
    "soap",
    "noodles",
    "shampoo sachets",
    "edible oil",
    "spices",
    "toothpaste",
)

# Free-text fragments a customer writes when short-paying, keyed by reason.
DEDUCTION_NOTES: dict[str, tuple[str, ...]] = {
    "damage": (
        "{n} cases damaged in transit",
        "breakage claim for {product} consignment",
        "{n} cartons received torn, claim adjusted",
    ),
    "promo": (
        "Q1 scheme discount adjusted",
        "trade scheme credit on {product}",
        "festival offer credit not yet received",
    ),
    "pricing": (
        "rate difference as per revised price list",
        "billed at old rate, difference deducted",
        "price protection on {product}",
    ),
    "short_ship": (
        "{n} cases short received",
        "short supply against PO",
        "quantity mismatch on delivery",
    ),
    "freight": (
        "freight borne by us as per terms",
        "transport charges adjusted",
        "octroi and freight deducted",
    ),
    "tds": (
        "TDS deducted u/s 194Q",
        "TDS as per Income Tax Act",
        "withholding tax adjusted",
    ),
}

# Bank channel prefixes that end up glued to the payer name.
CHANNELS: tuple[str, ...] = ("NEFT", "RTGS", "IMPS", "NEFT", "RTGS")
