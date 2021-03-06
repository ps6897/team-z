import json
from uuid import uuid4 as get_uuid
from itertools import chain
from contextlib import contextmanager

from sqlalchemy import create_engine, MetaData, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.engine import Engine
from sqlalchemy_utils import database_exists, create_database
from sqlalchemy.sql import Insert

from src.models import Base, Product, Location, BasketItem, PaymentType, Transaction
from src.config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_SECRET, MYSQL_DB
from src.cache import cache

engine: Engine = create_engine(
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_SECRET}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"
)
Session = sessionmaker(bind=engine)


# Deduplicates the products list so we are left with a list of unique products
def _deduplicate_products(li: list) -> list:

    # The dictionary is encoded serialized into json format and placed into a
    # set which cannot contain duplicate entries
    # Each json string is then transformed back into a dictionary and returned
    dumped_set = set([json.dumps(product, sort_keys=True) for product in li])
    return [json.loads(product) for product in dumped_set]


def get_unique_products(transactions: list) -> list:
    """
    Extract a list of unique products from the transactions list. Each
    product is assigned a UUID string

    Returns
    -------
    list
        A list containing the unique products as dictionaries
    """

    return [
        Product(**dict(product, **{"id": str(get_uuid())}))
        for product in _deduplicate_products(
            list(
                chain.from_iterable(
                    [transaction["basket"] for transaction in transactions]
                )
            )
        )
    ]


def get_locations(transactions: list) -> list:
    """
    Extract a list of unique locations from the transactions list. Each
    location is assigned a UUID string

    Returns
    -------
    list
        A list containing the unique locations as dictionaries
    """

    locations = [
        Location(id=str(get_uuid()), name=location)
        for location in set(transaction["location"] for transaction in transactions)
    ]
    return locations


def _get_existing_product_id(session, basket_item: dict) -> str:
    # Construct a key for the cache dictionary
    query_cache_key = ", ".join(
        (str(basket_item[key]) for key in ["name", "flavour", "size", "iced"])
    )
    # Attempt to retrieve the id from the cache
    result = cache.get(query_cache_key)
    if not result:
        # The query result isn't cached, so execute the query for the ID and add it to the cache
        result = (
            session.query(Product.id)
            .filter_by(
                name=basket_item["name"],
                flavour=basket_item["flavour"],
                size=basket_item["size"],
                iced=basket_item["iced"],
            )
            .one()[0]
        )
        cache.add(query_cache_key, result)

    return result


def get_basket_items(session, transactions: list) -> list:
    basket_items = []
    for transaction in transactions:
        basket_items += [
            BasketItem(
                id=str(get_uuid()),
                transaction_id=transaction["id"],
                product_id=_get_existing_product_id(session, basket_item),
            )
            for basket_item in transaction["basket"]
        ]

    return basket_items


def _get_existing_location_id(session, transaction: dict) -> str:
    query_cache_key = str(transaction["location"])

    result = cache.get(query_cache_key)
    if not result:
        result = (
            session.query(Location.id).filter_by(name=transaction["location"]).one()[0]
        )
        cache.add(query_cache_key, result)

    return result


def get_transactions(session, transactions: list) -> list:
    return [
        Transaction(
            id=transaction["id"],
            datetime=transaction["datetime"],
            payment_type=PaymentType.from_str(transaction["payment_type"]),
            card_details=transaction["card_details"],
            transaction_total=transaction["transaction_total"],
            location_id=_get_existing_location_id(session, transaction),
        )
        for transaction in transactions
    ]

# Listens for any before_execute event from SQLAlchemy
@event.listens_for(Engine, "before_execute", retval=True)
def _ignore_duplicate(conn, element, multiparams, params):
    # We only want to find any event which contains `ignore_tables` key in connection.info
    if (
        isinstance(element, Insert)
        and "ignore_tables" in conn.info
        and element.table.name in conn.info["ignore_tables"]
    ):
        # Prefix the query with IGNORE so that we can ignore duplicate inserts rather than
        # raising exception
        element = element.prefix_with("IGNORE")
    return element, multiparams, params


@contextmanager
def session_context_manager(ignore_tables=[]):
    """
    Context manager for SQLAlchemy session object, automatically commit changes
    and perform rollbacks on exception and close the connection

    Parameters
    ----------
    ignore_Tables: list
        A list of table names that use `INSERT IGNORE`

    Yields
    -------
    session
        SQLAlchemy Session object

    Example
    -------
    person = Person(id=uuid4(), first_name="John", last_name="Wrightson")
    with session_context_manager() as session:
        session.add(person)
    """

    session = Session()
    conn = session.connection()
    info = conn.info

    # Get the original ignore_tables dict object to be restored before `session.close()`
    previous = info.get("ignore_tables", ())

    try:
        # Set the ignore_tables from the `ignore_tables` param, for session block
        info["ignore_tables"] = set(ignore_tables)
        # Yield the session object to be used in the with statement
        yield session
        # When session exits scope without exception, the session is commited
        session.commit()
    except:
        # On exception, rollback any changes before raising the exception
        session.rollback()
        raise
    finally:
        # Always close the connection
        session.close()


def init():
    """
    Initialize the database by creating the database if it does not already
    exist & create all the defined tables
    """

    # Create database if it does not already exist
    if not database_exists(engine.url):
        create_database(engine.url)

    # Create all the tables
    Base.metadata.create_all(engine)


def insert_many(session, *argv):
    """
    Insert many rows into the database, each arg given must be an iterable
    containing some valid ORM class

    Parameters
    ----------
    session: Session
        The session object obtained from the `session_context_manager()` function
    """
    for data in argv:
        session.add_all(data)
