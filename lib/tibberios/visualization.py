from datetime import datetime

import pandas as pd
import plotly.express as px

from .core import Database


class GenerateViz:
    def __init__(self, db: Database) -> None:
        self._query_future_prices = r"""
            select
                start_time
                , unit_price
            from consumption
            where datetime(start_time) >= datetime(strftime('%Y-%m-%dT%H:00:00', 'now'))
                -- safeguard against unexpected future datapoints
                -- # BUG: something is fishy with the timezones on this one,
                -- # removing +1 day doesn't limit to today which likely means we would get data
                -- # from ~1 extra future day if they're available for some reason
                and datetime(start_time) <= datetime(strftime('%Y-%m-%dT23:59:59', 'now'), '+1 day')
            order by start_time;
        """
        self._db = db

    def _get_future_prices(self) -> None:
        price_df = pd.read_sql_query(
            sql=self._query_future_prices,
            con=self._db.connection,
        )
        price_df["start_time"] = pd.to_datetime(price_df["start_time"])

        # extract hour of the day
        price_df["hour"] = price_df["start_time"].apply(lambda t: t.strftime("%H"))
        self._price_df = price_df

    def _create_comparison_price(self, kwh: float, decimals: int = 0) -> None:
        self._price_df["comparison_price"] = (self._price_df["unit_price"] * kwh).round(
            decimals
        )

    def _generate_visualization(self) -> None:
        start_time_local = datetime.now().strftime(r"%d/%m %H:00")

        fig = px.bar(
            data_frame=self._price_df,
            x="start_time",
            y="unit_price",
            # TODO: Allow user to set comparison details
            labels={
                "start_time": "Start Hour",
                "unit_price": "Spot Price",
                "comparison_price": "25% LFP Charge",
            },
            # TODO: Allow user to set title
            title=f"Tibber Hourly Rates from {start_time_local} to {self._price_df.start_time.max().strftime(r'%d/%m %H:00')}<br>"
            + r"<sup>Prices above bars show estimated cost of adding 25% to ESS    "
            + f"<i>Updated at {datetime.now().strftime('%d/%m/%y %H:%M:%S')}</i></sup>",
            text="comparison_price",
            # TODO: Allow user to set output dimensions
            width=800,
            height=480,
            template="simple_white",
        )

        # naive check for if we have data for more than one date
        has_tomorrow = self._price_df.start_time.dt.date.nunique() == 2

        if has_tomorrow:
            # the 00 hour of the next day will always only occur once
            # since we've already filter the input data to be from the current hour on
            # and tibber releases next day prices around 1300
            next_day_start_time_index = self._price_df.loc[
                self._price_df["hour"] == "00", "start_time"
            ].index[0]
            fig.add_vrect(
                x0=self._price_df.start_time[next_day_start_time_index],
                # highlight until the end of the next day
                x1=self._price_df.start_time[self._price_df.shape[0] - 1],
                opacity=0.1,
                annotation_text="Tomorrow",
                annotation_position="top left",
            )

        # generate bi-hourly ticks
        start_times = []
        start_hours = []
        for index, data in self._price_df.iterrows():
            if not index % 2 or not has_tomorrow:
                start_times.append(data["start_time"])
                start_hours.append(data["hour"])
            else:
                start_times.append(None)
                start_hours.append(None)

        xaxis = dict(tickmode="array", tickvals=start_times, ticktext=start_hours)

        fig.update_layout(xaxis=xaxis)
        fig.update_traces(textposition="outside")
        fig.update_layout(xaxis_tickformat="%H:00")

        self._fig = fig

    def create_visualization(
        self, filepath: str, comparison_kwh: float, decimals: int
    ) -> None:
        self._get_future_prices()
        self._create_comparison_price(kwh=comparison_kwh, decimals=decimals)
        self._generate_visualization()
        self._fig.write_image(file=filepath, format=filepath.split(".")[-1])
