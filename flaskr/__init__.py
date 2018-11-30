import os
import pickle
import urllib3
import json
import pandas as pd
import warnings

from flask import Flask, render_template, request
from sklearn.preprocessing import MinMaxScaler
from wtforms import Form, StringField, validators, SelectField
from riotwatcher import RiotWatcher
from config.constants import REGIONS, API_KEY, CGG_API_KEY, POSITIONS

warnings.filterwarnings("ignore")


def create_app():
    # create and configure the app
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.urandom(16)
    )

    rw = RiotWatcher(API_KEY)
    current_version = rw.data_dragon.versions_for_region(region='euw')['v']
    # champs_raw = rw.data_dragon.champions(version=current_version)
    # champs = DataFrame(champs_raw['data']).T.reset_index(drop=True)[['id', 'name', 'key']]
    with open('champs.pickle', 'rb') as f:
        champs = pickle.load(f)
    champs['key'] = champs.key.astype(int)
    BOT = ['DUO_CARRY', 'DUO_SUPPORT']

    # ensure the instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    def matchups(champ_id, role):
        http = urllib3.PoolManager()
        url = 'api.champion.gg/v2/champions/{champ_id}/{role}/matchups?api_key={api_key}&limit={limit}'\
            .format(champ_id=champ_id, role=role, api_key=CGG_API_KEY, limit=champs.shape[0])
        r = http.request('GET', url)

        return json.loads(r.data)

    def get_counters_data(name, region, champ_id, position):
        # Get matchups for champ in position selected
        r = matchups(champ_id, position)
        df = pd.DataFrame(r)
        # Clean and structure the data
        if position not in BOT:
            c1 = df[df.champ1_id == champ_id]
            c1['winrate'] = c1.champ2.apply(lambda x: x['winrate'])
            c1 = c1.sort_values(by='winrate', ascending=False).drop(['champ1', 'champ1_id', '_id'], axis=1).reset_index(
                drop=True)
            c1 = c1.rename(columns={'champ2': 'champ_stats', 'champ2_id': 'champ_id'})

            c2 = df[df.champ2_id == champ_id]
            c2['winrate'] = c2.champ1.apply(lambda x: x['winrate'])
            c2 = c2.sort_values(by='winrate', ascending=False).drop(['champ2', 'champ2_id', '_id'], axis=1).reset_index(
                drop=True)
            c2 = c2.rename(columns={'champ1': 'champ_stats', 'champ1_id': 'champ_id'})

            df2 = pd.concat([c1, c2]).sort_values(by='winrate', ascending=False).reset_index(drop=True)
            df2.rename(columns={'count': 'games'}, inplace=True)
        else:
            c1 = df[(df.champ1_id == champ_id) & (df.role)]
            c1['winrate'] = c1.champ2.apply(lambda x: x['winrate'])
            c1 = c1.sort_values(by='winrate', ascending=False).drop(['champ1', 'champ1_id', '_id'], axis=1).reset_index(
                drop=True)
            c1 = c1.rename(columns={'champ2': 'champ_stats', 'champ2_id': 'champ_id'})

            c2 = df[df.champ2_id == champ_id]
            c2['winrate'] = c2.champ1.apply(lambda x: x['winrate'])
            c2 = c2.sort_values(by='winrate', ascending=False).drop(['champ2', 'champ2_id', '_id'], axis=1).reset_index(
                drop=True)
            c2 = c2.rename(columns={'champ1': 'champ_stats', 'champ1_id': 'champ_id'})

            df2 = pd.concat([c1, c2]).sort_values(by='winrate', ascending=False).reset_index(drop=True)
            df2.rename(columns={'count': 'games'}, inplace=True)

        # Matchups with 100 or more encounters and with more than 50% winrate
        df3 = df2[(df2.games >= 100) & (df2.winrate > .47)]
        actual_counters_df = df3.merge(champs, left_on='champ_id', right_on='key', how='left').drop(['id', 'key'],
                                                                                                    axis=1)

        # Check how good the player is with that champs
        summ = rw.summoner.by_name(region=region, summoner_name=name)
        mp = rw.champion_mastery.by_summoner(summoner_id=summ['id'], region=region)
        mp_df = pd.DataFrame(mp)
        mp_df = mp_df.drop(['championPointsSinceLastLevel', 'championPointsUntilNextLevel', 'chestGranted', 'playerId',
                            'tokensEarned'], axis=1).sort_values('championPoints', ascending=False)

        # Mix the data from both sources
        df4 = actual_counters_df.merge(mp_df, left_on='champ_id', right_on='championId', how='left').drop('championId',
                                                                                                          axis=1)
        df4.fillna(0, inplace=True)

        mm = MinMaxScaler()
        df4['cpoints_scaled'] = mm.fit_transform(df4.championPoints)
        df4['clvl_scaled'] = mm.fit_transform(df4.championLevel)
        df4['wr_scaled'] = mm.fit_transform(df4.winrate)

        # Set the relevance of every champ
        df4['relevance'] = df4.cpoints_scaled + df4.clvl_scaled + df4.wr_scaled
        df5 = df4.sort_values(by='relevance', ascending=False)
        result = df5[['champ_id', 'name', 'winrate', 'games', 'championPoints', 'championLevel', 'relevance']]

        return result, summ['profileIconId']

    class LCPForm(Form):
        name = StringField('Summoner name', [validators.Length(min=3, max=26)])
        region = SelectField('Region', choices=[(v, k) for k, v in REGIONS.items()])
        champion = SelectField('Champion to counter', choices=[(str(d['key']), d['name']) for d in
                                                    champs[['key', 'name']].to_dict(orient='records')])
        position = SelectField('Position', choices=[('TOP', 'Top'), ('JUNGLE', 'Jungle'), ('MIDDLE', 'Middle'),
                                                    ('DUO_CARRY', 'ADC'), ('DUO_SUPPORT', 'Support')])

    def get_position(pos):
        return POSITIONS[pos]

    @app.route("/", methods=['GET', 'POST'])
    def index():
        form = LCPForm(request.form)
        if request.method == 'POST' and form.validate():
            counter_champs, icon_id = get_counters_data(form.name.data, form.region.data, int(form.champion.data),
                                                        form.position.data)
            enumerate_champs = enumerate(counter_champs.to_dict(orient='records'))
            position = get_position(form.position.data)
            return render_template('counters.html', counter_champs=enumerate_champs,
                                   icon_id=icon_id, patch=current_version, summoner_name=form.name.data,
                                   champ_name=champs[champs.key == int(form.champion.data)]['name'].tolist()[0],
                                   champ_id=form.champion.data, position=position)
        return render_template('index.html', form=form)

    return app
