# SPDX-FileCopyrightText: : 2017-2020 The PyPSA-Eur Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# coding: utf-8
"""
Creates networks clustered to ``{cluster}`` number of zones with aggregated buses, generators and transmission corridors.

Relevant Settings
-----------------

.. code:: yaml

    focus_weights:

    renewable: (keys)
        {technology}:
            potential:

    solving:
        solver:
            name:

    lines:
        length_factor:

.. seealso::
    Documentation of the configuration file ``config.yaml`` at
    :ref:`toplevel_cf`, :ref:`renewable_cf`, :ref:`solving_cf`, :ref:`lines_cf`

Inputs
------

- ``resources/regions_onshore_elec_s{simpl}.geojson``: confer :ref:`simplify`
- ``resources/regions_offshore_elec_s{simpl}.geojson``: confer :ref:`simplify`
- ``resources/busmap_elec_s{simpl}.csv``: confer :ref:`simplify`
- ``networks/elec_s{simpl}.nc``: confer :ref:`simplify`
- ``data/custom_busmap_elec_s{simpl}_{clusters}.csv``: optional input

Outputs
-------

- ``resources/regions_onshore_elec_s{simpl}_{clusters}.geojson``:

    .. image:: ../img/regions_onshore_elec_s_X.png
        :scale: 33 %

- ``resources/regions_offshore_elec_s{simpl}_{clusters}.geojson``:

    .. image:: ../img/regions_offshore_elec_s_X.png
        :scale: 33 %

- ``resources/busmap_elec_s{simpl}_{clusters}.csv``: Mapping of buses from ``networks/elec_s{simpl}.nc`` to ``networks/elec_s{simpl}_{clusters}.nc``;
- ``resources/linemap_elec_s{simpl}_{clusters}.csv``: Mapping of lines from ``networks/elec_s{simpl}.nc`` to ``networks/elec_s{simpl}_{clusters}.nc``;
- ``networks/elec_s{simpl}_{clusters}.nc``:

    .. image:: ../img/elec_s_X.png
        :scale: 40  %

Description
-----------

.. note::

    **Why is clustering used both in** ``simplify_network`` **and** ``cluster_network`` **?**

        Consider for example a network ``networks/elec_s100_50.nc`` in which
        ``simplify_network`` clusters the network to 100 buses and in a second
        step ``cluster_network``` reduces it down to 50 buses.

        In preliminary tests, it turns out, that the principal effect of
        changing spatial resolution is actually only partially due to the
        transmission network. It is more important to differentiate between
        wind generators with higher capacity factors from those with lower
        capacity factors, i.e. to have a higher spatial resolution in the
        renewable generation than in the number of buses.

        The two-step clustering allows to study this effect by looking at
        networks like ``networks/elec_s100_50m.nc``. Note the additional
        ``m`` in the ``{cluster}`` wildcard. So in the example network
        there are still up to 100 different wind generators.

        In combination these two features allow you to study the spatial
        resolution of the transmission network separately from the
        spatial resolution of renewable generators.

    **Is it possible to run the model without the** ``simplify_network`` **rule?**

        No, the network clustering methods in the PyPSA module
        `pypsa.networkclustering <https://github.com/PyPSA/PyPSA/blob/master/pypsa/networkclustering.py>`_
        do not work reliably with multiple voltage levels and transformers.

.. tip::
    The rule :mod:`cluster_all_networks` runs
    for all ``scenario`` s in the configuration file
    the rule :mod:`cluster_network`.

Exemplary unsolved network clustered to 512 nodes:

.. image:: ../img/elec_s_512.png
    :scale: 40  %
    :align: center

Exemplary unsolved network clustered to 256 nodes:

.. image:: ../img/elec_s_256.png
    :scale: 40  %
    :align: center

Exemplary unsolved network clustered to 128 nodes:

.. image:: ../img/elec_s_128.png
    :scale: 40  %
    :align: center

Exemplary unsolved network clustered to 37 nodes:

.. image:: ../img/elec_s_37.png
    :scale: 40  %
    :align: center

"""

import logging
from _helpers import configure_logging

import pypsa
import os
import shapely

import pandas as pd
import numpy as np
import geopandas as gpd
import pyomo.environ as po
import matplotlib.pyplot as plt
import seaborn as sns
import scipy as sp

import networkx as nx
#import community

from sklearn.cluster import AgglomerativeClustering
from six.moves import reduce
from vresutils.benchmark import memory_logger

from pypsa.networkclustering import (busmap_by_kmeans, busmap_by_spectral_clustering,
                                     _make_consense, get_clustering_from_busmap)

from add_electricity import load_costs
from newman import greedy_modularity

idx = pd.IndexSlice

logger = logging.getLogger(__name__)


def normed(x): return (x/x.sum()).fillna(0.)


def weighting_for_country(n, x):
    conv_carriers = {'OCGT','CCGT','PHS', 'hydro'}
    gen = (n
           .generators.loc[n.generators.carrier.isin(conv_carriers)]
           .groupby('bus').p_nom.sum()
           .reindex(n.buses.index, fill_value=0.) +
           n
           .storage_units.loc[n.storage_units.carrier.isin(conv_carriers)]
           .groupby('bus').p_nom.sum()
           .reindex(n.buses.index, fill_value=0.))
    load = n.loads_t.p_set.mean().groupby(n.loads.bus).sum()

    b_i = x.index
    g = normed(gen.reindex(b_i, fill_value=0))
    l = normed(load.reindex(b_i, fill_value=0))

    w = g + l
    return (w * (100. / w.max())).clip(lower=1.).astype(int)


def distribute_clusters(n, n_clusters, focus_weights=None, solver_name=None):
    """Determine the number of clusters per country"""

    if solver_name is None:
        solver_name = snakemake.config['solving']['solver']['name']

    L = (n.loads_t.p_set.mean()
         .groupby(n.loads.bus).sum()
         .groupby([n.buses.country, n.buses.sub_network]).sum()
         .pipe(normed))

    N = n.buses.groupby(['country', 'sub_network']).size()

    assert n_clusters >= len(N) and n_clusters <= N.sum(), \
        f"Number of clusters must be {len(N)} <= n_clusters <= {N.sum()} for this selection of countries."

    if focus_weights is not None:

        total_focus = sum(list(focus_weights.values()))

        assert total_focus <= 1.0, "The sum of focus weights must be less than or equal to 1."

        for country, weight in focus_weights.items():
            L[country] = weight / len(L[country])

        remainder = [c not in focus_weights.keys() for c in L.index.get_level_values('country')]
        L[remainder] = L.loc[remainder].pipe(normed) * (1 - total_focus)

        logger.warning('Using custom focus weights for determining number of clusters.')

    assert np.isclose(L.sum(), 1.0, rtol=1e-3), f"Country weights L must sum up to 1.0 when distributing clusters. Is {L.sum()}."

    m = po.ConcreteModel()
    def n_bounds(model, *n_id):
        return (1, N[n_id])
    m.n = po.Var(list(L.index), bounds=n_bounds, domain=po.Integers)
    m.tot = po.Constraint(expr=(po.summation(m.n) == n_clusters))
    m.objective = po.Objective(expr=sum((m.n[i] - L.loc[i]*n_clusters)**2 for i in L.index),
                               sense=po.minimize)

    opt = po.SolverFactory(solver_name)
    if not opt.has_capability('quadratic_objective'):
        logger.warning(f'The configured solver `{solver_name}` does not support quadratic objectives. Falling back to `ipopt`.')
        opt = po.SolverFactory('ipopt')

    results = opt.solve(m)
    assert results['Solver'][0]['Status'] == 'ok', f"Solver returned non-optimally: {results}"

    return pd.Series(m.n.get_values(), index=L.index).astype(int)


def busmap_for_n_clusters(n, n_clusters, solver_name, focus_weights=None, algorithm="kmeans", **algorithm_kwds):
    if algorithm == "kmeans":
        algorithm_kwds.setdefault('n_init', 1000)
        algorithm_kwds.setdefault('max_iter', 30000)
        algorithm_kwds.setdefault('tol', 1e-6)

    n.determine_network_topology()

    n_clusters = distribute_clusters(n, n_clusters, focus_weights=focus_weights, solver_name=solver_name)

    def reduce_network(n, buses):
        nr = pypsa.Network()
        nr.import_components_from_dataframe(buses, "Bus")
        nr.import_components_from_dataframe(n.lines.loc[n.lines.bus0.isin(buses.index) & n.lines.bus1.isin(buses.index)], "Line")
        return nr

    #the following functions "busmap_by_hac", "busmap_by_newman" and "busmap_by_louvain" should go to PyPSA!
    def busmap_by_hac(n, n_clusters, buses_i, feature=None):
        carrier = feature.split('-')[0].split('+')
        
        if feature.split('-')[1] == 'cap':
            #data = n.generators_t.p_max_pu.filter(like=feature.split('-')[0]).mean().rename(index=lambda x: x.split(' ')[0]).loc[buses_i]
            #data = data.reset_index().drop('name', axis=1)
            #data.index = data.index.astype('str')
            data = pd.DataFrame(n.generators_t.p_max_pu.filter(like=carrier[0]).mean().rename(index=lambda x: x.split(' ')[0]).loc[buses_i], index=buses_i, columns=[carrier[0]])
            if len(carrier)>1:
                for c in carrier[1:]:
                    data[c] = n.generators_t.p_max_pu.filter(like=c).mean().rename(index=lambda x: x.split(' ')[0]).loc[buses_i]
        if feature.split('-')[1] == 'time':
            #data = n.generators_t.p_max_pu.filter(like=feature.split('-')[0]).T.rename(index=lambda x: x.split(' ')[0]).loc[buses_i]
            #data = data.reset_index().drop('name', axis=1)
            #data.index = data.index.astype('str')
            data = n.generators_t.p_max_pu.filter(like=carrier[0]).rename(columns=lambda x: x.split(' ')[0])[buses_i]
            if len(carrier)>1:
                for c in carrier[1:]:
                    data=data.append(n.generators_t.p_max_pu.filter(like=c).rename(columns=lambda x: x.split(' ')[0])[buses_i])
            data = data.T
    
        buses_x = n.buses.index.get_indexer(buses_i)

        adj = n.adjacency_matrix(branch_components=['Line']).todense()
        adj = sp.sparse.coo_matrix(adj[buses_x].T[buses_x].T)
            
        labels = AgglomerativeClustering(n_clusters=n_clusters,
                                         connectivity=adj,
                                         affinity='euclidean',
                                         linkage='ward').fit_predict(data)
    
        busmap = pd.Series(data=labels, index=buses_i, dtype='str')
            
        return busmap

    def busmap_by_louvain(network, n_clusters, buses_i):
        network.calculate_dependent_values()
    
        lines = (network.lines[network.lines.bus0.isin(buses_i) & network.lines.bus1.isin(buses_i)]
                 .loc[:,['bus0', 'bus1']].assign(weight=1./network.lines.x).set_index(['bus0','bus1']))
        lines.weight+=0.1
    
        G = nx.Graph()
        G.add_nodes_from(network.buses.loc[buses_i].index)
        G.add_edges_from((u,v,dict(weight=w)) for (u,v),w in lines.itertuples())
    
        for repeat in range(0,3): #repeat 3x, to avoid failure by chance
            res = 100/(n_clusters**1.5) #default
            
            c = 1
            b=community.best_partition(G, resolution=res)
            bu = b.copy()
            res_vals = []
            while (len(pd.Series(b).unique()) != n_clusters) & (c < 500):
                if len(pd.Series(b).unique()) < n_clusters:
                    b=community.best_partition(G, resolution=res)
                    res /= (c**2)
                if len(pd.Series(b).unique()) > n_clusters:
                    b=community.best_partition(G, resolution=res)
                    res += 1/(c**1.5)
                if res < 1e-4:
                    res += 1e-4
                
                if abs(len(pd.Series(b).unique()) - n_clusters) <= abs(len(pd.Series(bu).unique()) - n_clusters):
                    bu = b.copy()

                c += 1
                if (len(pd.Series(bu).unique()) == n_clusters):
                    break
        
            list_cluster=[]
            for i in bu:
                list_cluster.append(str(bu[i]))
        
        return pd.Series(list_cluster,index=network.buses.loc[buses_i].index)

    def busmap_by_newman(n, n_clusters, buses_i):
        n.calculate_dependent_values()
    
        lines = n.lines[(n.lines.bus0.isin(buses_i)) & (n.lines.bus1.isin(buses_i))]
        lines = lines.loc[:,['bus0', 'bus1']].assign(weight=n.lines.s_nom/abs(1j*lines.r)).set_index(['bus0','bus1'])
    
        G = nx.Graph()
        G.add_nodes_from(buses_i)
        G.add_edges_from((u,v,dict(weight=w)) for (u,v),w in lines.itertuples())
        
        output = greedy_modularity(G, n_clusters, weight='weight')
        busmap = pd.Series(buses_i, buses_i)
        for c in np.arange(len(output)):
            busmap.loc[output[c]] = str(c)
        busmap.index = busmap.index.astype(str)
        return busmap

        
    ## return to PyPSA-EUR

    def busmap_for_country(x):
        prefix = x.name[0] + x.name[1] + ' '
        logger.debug(f"Determining busmap for country {prefix[:-1]}")
        if len(x) == 1:
            return pd.Series(prefix + '0', index=x.index)
        weight = weighting_for_country(n, x)

        if algorithm == "kmeans":
            return prefix + busmap_by_kmeans(n, weight, n_clusters[x.name], buses_i=x.index, **algorithm_kwds)
        elif algorithm == "spectral":
            return prefix + busmap_by_spectral_clustering(reduce_network(n, x), n_clusters[x.name], **algorithm_kwds)
        #elif algorithm == "louvain":
        #    return prefix + busmap_by_louvain(reduce_network(n, x), n_clusters[x.name], **algorithm_kwds)
        elif algorithm == "hac":
            return prefix + busmap_by_hac(n, n_clusters[x.name], buses_i=x.index, feature=snakemake.config['clustering']['feature'])
        elif algorithm == "louvain":
            return prefix + busmap_by_louvain(n, n_clusters[x.name], buses_i=x.index)
        elif algorithm == "newman":
            return prefix + busmap_by_newman(n, n_clusters[x.name], buses_i=x.index)
        else:
            raise ValueError(f"`algorithm` must be one of 'kmeans', 'spectral' or 'louvain'. Is {algorithm}.")

    return (n.buses.groupby(['country', 'sub_network'], group_keys=False)
            .apply(busmap_for_country).squeeze().rename('busmap'))


def clustering_for_n_clusters(n, n_clusters, custom_busmap=False, aggregate_carriers=None,
                              line_length_factor=1.25, potential_mode='simple', solver_name="cbc",
                              algorithm="kmeans", extended_link_costs=0, focus_weights=None):

    if potential_mode == 'simple':
        p_nom_max_strategy = np.sum
    elif potential_mode == 'conservative':
        p_nom_max_strategy = np.min
    else:
        raise AttributeError(f"potential_mode should be one of 'simple' or 'conservative' but is '{potential_mode}'")

    if custom_busmap:
        busmap = pd.read_csv(snakemake.input.custom_busmap, index_col=0, squeeze=True)
        busmap.index = busmap.index.astype(str)
        logger.info(f"Imported custom busmap from {snakemake.input.custom_busmap}")
    else:
        logger.info(f'Generating busmap using {algorithm}...')
        busmap = busmap_for_n_clusters(n, n_clusters, solver_name, focus_weights, algorithm)
        print(n_clusters)
        n_clusters = len(busmap.unique())
        print(n_clusters)

    clustering = get_clustering_from_busmap(
        n, busmap,
        bus_strategies=dict(country=_make_consense("Bus", "country")),
        aggregate_generators_weighted=True,
        aggregate_generators_carriers=aggregate_carriers,
        aggregate_one_ports=["Load", "StorageUnit"],
        line_length_factor=line_length_factor,
        generator_strategies={'p_nom_max': p_nom_max_strategy},
        scale_link_capital_costs=False)

    if not n.links.empty:
        nc = clustering.network
        nc.links['underwater_fraction'] = (n.links.eval('underwater_fraction * length')
                                        .div(nc.links.length).dropna())
        nc.links['capital_cost'] = (nc.links['capital_cost']
                                    .add((nc.links.length - n.links.length)
                                        .clip(lower=0).mul(extended_link_costs),
                                        fill_value=0))

    return clustering


def save_to_geojson(s, fn):
    if os.path.exists(fn):
        os.unlink(fn)
    df = s.reset_index()
    schema = {**gpd.io.file.infer_schema(df), 'geometry': 'Unknown'}
    df.to_file(fn, driver='GeoJSON', schema=schema)


def cluster_regions(busmaps, input=None, output=None):
    if input is None: input = snakemake.input
    if output is None: output = snakemake.output

    busmap = reduce(lambda x, y: x.map(y), busmaps[1:], busmaps[0])

    for which in ('regions_onshore', 'regions_offshore'):
        regions = gpd.read_file(getattr(input, which)).set_index('name')
        geom_c = regions.geometry.groupby(busmap).apply(shapely.ops.cascaded_union)
        regions_c = gpd.GeoDataFrame(dict(geometry=geom_c))
        regions_c.index.name = 'name'
        save_to_geojson(regions_c, getattr(output, which))


def plot_busmap_for_n_clusters(n, n_clusters, fn=None):
    busmap = busmap_for_n_clusters(n, n_clusters)
    cs = busmap.unique()
    cr = sns.color_palette("hls", len(cs))
    n.plot(bus_colors=busmap.map(dict(zip(cs, cr))))
    if fn is not None:
        plt.savefig(fn, bbox_inches='tight')
    del cs, cr


if __name__ == "__main__":
    if 'snakemake' not in globals():
        from _helpers import mock_snakemake
        snakemake = mock_snakemake('cluster_network', network='elec', simpl='', clusters='5')
    configure_logging(snakemake)

    n = pypsa.Network(snakemake.input.network)

    focus_weights = snakemake.config.get('focus_weights', None)

    renewable_carriers = pd.Index([tech
                                   for tech in n.generators.carrier.unique()
                                   if tech in snakemake.config['renewable']])

    if snakemake.wildcards.clusters.endswith('m'):
        n_clusters = int(snakemake.wildcards.clusters[:-1])
        aggregate_carriers = pd.Index(n.generators.carrier.unique()).difference(renewable_carriers)
    else:
        n_clusters = int(snakemake.wildcards.clusters)
        aggregate_carriers = None # All

    if n_clusters == len(n.buses):
        # Fast-path if no clustering is necessary
        busmap = n.buses.index.to_series()
        linemap = n.lines.index.to_series()
        clustering = pypsa.networkclustering.Clustering(n, busmap, linemap, linemap, pd.Series(dtype='O'))
    else:
        line_length_factor = snakemake.config['lines']['length_factor']
        hvac_overhead_cost = (load_costs(n.snapshot_weightings.sum()/8760,
                                   tech_costs=snakemake.input.tech_costs,
                                   config=snakemake.config['costs'],
                                   elec_config=snakemake.config['electricity'])
                              .at['HVAC overhead', 'capital_cost'])

        def consense(x):
            v = x.iat[0]
            assert ((x == v).all() or x.isnull().all()), (
                "The `potential` configuration option must agree for all renewable carriers, for now!"
            )
            return v
        potential_mode = consense(pd.Series([snakemake.config['renewable'][tech]['potential']
                                             for tech in renewable_carriers]))
        custom_busmap = snakemake.config["enable"].get("custom_busmap", False)
        
        fn = getattr(snakemake.log, 'memory', None)
        with memory_logger(filename=fn, interval=10.) as mem:
            clustering = clustering_for_n_clusters(n, n_clusters, custom_busmap, aggregate_carriers,
                                                   line_length_factor=line_length_factor,
                                                   potential_mode=potential_mode,
                                                   solver_name=snakemake.config['solving']['solver']['name'],
                                                   algorithm=snakemake.config['clustering']['algorithm'],
                                                   extended_link_costs=hvac_overhead_cost,
                                                   focus_weights=focus_weights)
        logger.info("Maximum memory usage for clustering: {}".format(mem.mem_usage))

    clustering.network.export_to_netcdf(snakemake.output.network)
    for attr in ('busmap', 'linemap'): #also available: linemap_positive, linemap_negative
        getattr(clustering, attr).to_csv(snakemake.output[attr])

    cluster_regions((clustering.busmap,))
