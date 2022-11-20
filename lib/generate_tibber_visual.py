import boto3

from .tibberios.core import Database, TibberConnector
from .tibberios.visualization import GenerateViz
from .constants import dotenv_config


async def main():
    resolution = "HOURLY"
    records = 24 * 2

    db = Database('tibber.db')
    tib = TibberConnector(dotenv_config("TIBBER_ACCESS_TOKEN"))
    price_data = await tib.get_price_data(resolution=resolution, records=records)

    tbl_name = "consumption"
    columns = {
        "start_time": "DATE PRIMARY KEY",
        "unit_price": "REAL",
        "total_cost": "REAL",
        "cost": "REAL",
        "consumption": "REAL",
    }
    pk = "start_time"

    db.create_table(name=tbl_name, cols_n_types=columns)
    db.upsert_table(name=tbl_name, columns=columns.keys(), values=price_data.price_table, pk=pk)
    db.delete_null_rows(name=tbl_name, pk=pk)

    gv = GenerateViz(db)

    print(f"Tibber Graph Generator: Generating visualization...")
    gv.create_visualization(filepath="prices.png", comparison_kwh=13, decimals=1)

    db.close()

    # upload to s3 bucket
    s3 = boto3.client('s3', aws_access_key_id=dotenv_config("AWS_ACCESS_KEY"),
                      aws_secret_access_key=dotenv_config("AWS_SECRET_KEY"))

    print(f"Tibber Graph Generator: Uploading to s3 bucket and setting ACL...")

    with open("prices.png", "rb") as file:
        s3.upload_fileobj(file, "tibber-graphs", "prices.png",
                          ExtraArgs={'ContentType': "image/png", 'ACL': 'public-read'})

    print(f"Tibber Graph Generator:  Finished. Sleeping 1h...")

def run():
    from asyncio import run as async_run
    async_run(main())
    exit(0)


if __name__ == "__main__":
    run()
