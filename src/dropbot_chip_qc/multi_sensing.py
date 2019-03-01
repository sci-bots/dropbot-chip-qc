# -*- encoding: utf-8 -*-
'''
Quality control functions using DropBot multi-sensing.

Requires `dropbot>=2.2.0`.
'''
from __future__ import print_function, absolute_import
import itertools as it
import logging
import time

import dropbot as db
import dropbot.dispense
import dropbot.move
import functools as ft
import networkx as nx
import numpy as np
import pandas as pd
import trollius as asyncio
import winsound

from .. import __version__


@asyncio.coroutine
def _run_test(signals, proxy, G, way_points, start=None):
    '''
    Signals
    -------

    The following signals are sent during the test:

    * ``test-start``; test has started:

      - ``route``: planned list of electrodes to visit consecutively
      - ``way_points``: contiguous list of waypoints, where test is routed as
        the shortest path between each consecutive pair of waypoints

    * ``electrode-success``; movement of liquid to electrode has
      completed:

      - ``source``: electrode where liquid is moving **from**
      - ``target``: electrode where liquid is moving **to**
      - ``start``: **start** time for electrode movement attempt
      - ``end``: **end** time for electrode movement attempt
      - ``attempt``: attempts required for successful movement

    * ``electrode-attempt-fail``; single attempt to move liquid to target
      electrode has failed:

      - ``source``: electrode where liquid is moving **from**
      - ``target``: electrode where liquid is moving **to**
      - ``start``: **start** time for electrode movement attempt
      - ``end``: **end** time for electrode movement attempt
      - ``attempt``: attempts required for successful movement

    * ``electrode-fail``; movement of liquid to electrode has failed:

      - ``source``: electrode where liquid is moving **from**
      - ``target``: electrode where liquid is moving **to**
      - ``start``: **start** time for electrode movement attempt
      - ``end``: **end** time for electrode movement attempt
      - ``attempt``: attempts made for electrode movement

    * ``electrode-skip``; skip unreachable electrode:

      - ``source``: electrode where liquid is moving **from**
      - ``target``: electrode where liquid is moving **to**

    * ``test-complete``; test has completed:

      - ``success_route``: list of electrodes visited consecutively
      - ``failed_electrodes``: list of electrodes where movement failed
      - ``success_electrodes``: list of electrodes where movement succeeded
      - ``__version__``: :py:mod:`dropbot_chip_qc` package version


    Returns
    -------
    dict
        Test summary including the same fields as the ``test-complete`` signal
        above.


    .. versionchanged:: 0.3
        Send the following signals: ``electrode-success``,
        ``electrode-attempt-fail``, ``electrode-fail``, ``test-complete``.
    .. versionchanged:: 0.3
        Rename results dictionary keys::
        - ``route`` -> ``success_route``, i.e., actual route taken including
          re-routes
        - ``failed_nodes`` -> ``failed_electrodes``
        - ``success_nodes`` -> ``success_electrodes``
    .. versionchanged:: 0.5
        Send the ``electrode-skip`` signal.
    .. versionchanged:: 0.5
        Prune unreachable electrodes from test route (e.g., after liquid
        movement to a bottleneck electrode has failed; cutting off the only
        path to other electrodes on the test route).
    '''
    logging.info('Begin DMF chip test routine.')
    G_i = G.copy()
    # XXX TODO Remove channel mapping for electrodes 89 and 30 in `SCI-BOTS
    # 90-pin array` device SVG.
    G_i.remove_node(89)
    G_i.remove_node(30)

    if start is None:
        start = way_points[0]
    way_points_i = np.roll(way_points, -way_points.index(start)).tolist()
    way_points_i += [way_points[0]]

    route = list(it.chain(*[nx.shortest_path(G_i, source, target)[:-1]
                            for source, target in db.move
                            .window(way_points_i, 2)])) + [way_points_i[-1]]

    signals.signal('test-start').send('_run_test', route=route,
                                      way_points=way_points_i)

    remaining_route_i = route[:]
    success_route = route[:1]

    while len(remaining_route_i) > 1:
        # Attempt to move liquid from first electrode to second electrode.
        # If liquid movement fails:
        #  * Alert operator (e.g., log notification, alert sound, etc.)
        #  * Attempt to "route around" failed electrode
        source_i = remaining_route_i.pop(0)

        while remaining_route_i[0] not in G_i:
            remaining_route_i.pop(0)
            try:
                remaining_route_i = (nx.shortest_path(G_i, source_i,
                                                      remaining_route_i[0]) +
                                     remaining_route_i[1:])
            except (nx.NetworkXNoPath, nx.NodeNotFound) as exception:
                if len(remaining_route_i) < 2:
                    raise
                elif remaining_route_i[0] in G_i:
                    # Skip unreachable electrode.  This can happen, e.g., if a
                    # failed electrode is identified and removed, cutting off
                    # the only path to other electrodes on route.
                    G_i.remove_node(remaining_route_i[0])
                    signals.signal('electrode-skip')\
                        .send('_run_test', source=source_i,
                              target=remaining_route_i[0])
                    logging.warning('Pruning unreachable electrode: `%s`',
                                    remaining_route_i[0])
        target_i = remaining_route_i[0]

        start_time = time.time()
        for i in range(4):
            try:
                yield asyncio.From(db.dispense
                                   .move_liquid(proxy, [source_i, target_i],
                                                wrapper=ft.partial(asyncio
                                                                   .wait_for,
                                                                   timeout=2)))
                success_route.append(target_i)
                signals.signal('electrode-success').send('_run_test',
                                                         source=source_i,
                                                         target=target_i,
                                                         start=start_time,
                                                         end=time.time(),
                                                         attempt=i + 1)
                break
            except db.move.MoveTimeout as exception:
                logging.warning('Timed out moving liquid `%s`->`%s`' %
                                tuple(exception.route_i))
                db.dispense.apply_duty_cycles(proxy, pd.Series(1, index=[source]))
                signals.signal('electrode-attempt-fail')\
                    .send('_run_test', source=source_i, target=target_i,
                          start=start_time, end=time.time(), attempt=i + 1)
                time.sleep(1.)
        else:
            # Play system "beep" sound to notify user that electrode failed.
            winsound.MessageBeep()
            logging.error('Failed to move liquid to electrode `%s`.', target_i)
            signals.signal('electrode-fail').send('_run_test',
                                                  source=source_i,
                                                  target=target_i,
                                                  start=start_time,
                                                  end=time.time(),
                                                  attempt=i + 1)
            # Remove failed electrode adjacency graph.
            G_i.remove_node(target_i)
            remaining_route_i = [source_i] + remaining_route_i
            logging.warning('Attempting to reroute around electrode `%s`.',
                            target_i)
        yield asyncio.From(asyncio.sleep(0))

    db.dispense.apply_duty_cycles(proxy, pd.Series(1, index=remaining_route_i))
    # Play system "beep" sound to notify user that electrode failed.
    winsound.MessageBeep()
    result = {'success_route': success_route,
              'failed_electrodes': sorted(set(route) - set(success_route)),
              'success_electrodes': sorted(set(success_route)),
              '__version__': __version__}
    logging.info('Completed - failed electrodes: `%s`' %
                 result['failed_electrodes'])
    signals.signal('test-complete').send('_run_test', **result)
    raise asyncio.Return(result)
