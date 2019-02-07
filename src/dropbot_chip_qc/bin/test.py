from __future__ import print_function, absolute_import
import argparse
import itertools as it
import json
import logging
import sys
import threading
import time

from asyncio_helpers import cancellable
from PySide2.QtWidgets import QMessageBox, QMainWindow, QApplication
import blinker
import dropbot as db
import dropbot.dispense
import functools as ft
import mutagen
import networkx as nx
import numpy as np
import pandas as pd
import path_helpers as ph
import trollius as asyncio
import winsound

from ..connect import connect
from ..video import chip_video_process, show_chip


def question(text, title='Question', flags=QMessageBox.StandardButton.Yes |
             QMessageBox.StandardButton.No):
    return QMessageBox.question(QMainWindow(), title, text, flags)


def try_async(*args, **kwargs):
    loop = asyncio.get_event_loop()
    kwargs['timeout'] = kwargs.get('timeout', 5)
    return loop.run_until_complete(asyncio.wait_for(*args, **kwargs))


def try_async_co(*args, **kwargs):
    kwargs['timeout'] = kwargs.get('timeout', 5)
    return asyncio.wait_for(*args, **kwargs)


def run_test(way_points, start_electrode, video_dir=None):
    if video_dir is not None:
        video_dir = ph.path(video_dir)

    ready = threading.Event()
    closed = threading.Event()

    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)

    signals = blinker.Namespace()

    signals.signal('closed').connect(lambda sender: closed.set(), weak=False)

    monitor_task, proxy, G = connect()
    proxy.voltage = 115

    def update_video(video, uuid, json_results_path):
        json_results_path = ph.path(json_results_path)
        response = question('Attempt to set UUID in title of video file, '
                            '`%s`?' % video, title='Update video?')
        if response == QMessageBox.StandardButton.Yes:
            try:
                f = mutagen.File(video)
                if ('\xa9nam' not in f.tags) or ('UUID' not in
                                                 f.tags['\xa9nam']):
                    f.tags['\xa9nam'] = \
                        'DMF chip QC - UUID: %s' % uuid
                    f.save()
                    logging.info('wrote UUID to video title: `%s`', video)
            except Exception:
                logging.warning('Error setting video title.',
                                exc_info=True)
            output_video_path = (json_results_path.parent
                                 .joinpath('%s.mp4' %
                                           json_results_path.namebase))
            ph.path(video).move(output_video_path)
            logging.info('moved video to : `%s`', output_video_path)

    def on_chip_detected(sender, **kwargs):
        @ft.wraps(on_chip_detected)
        def wrapped(sender, **kwargs):
            signals.signal('chip-detected').disconnect(on_chip_detected)
            uuid = kwargs['decoded_objects'][0].data
            ready.uuid = uuid
            ready.set()

            db.dispense.apply_duty_cycles(proxy,
                                          pd.Series(1, index=[start_electrode]))

            # Wait for chip to be detected.
            response = None

            while response != QMessageBox.StandardButton.Yes:
                response = question('Chip detected: `%s`.\n\nLiquid loaded '
                                    'into electrode %s?' % (uuid,
                                                            start_electrode),
                                    title='Chip detected')

            proxy.stop_switching_matrix()
            proxy.turn_off_all_channels()

            @asyncio.coroutine
            def _run():
                try:
                    start = time.time()
                    result = \
                        yield asyncio.From(_run_test(signals, proxy, G,
                                                     way_points,
                                                     start=start_electrode))
                    output_path = '%s - qc results.json' % uuid
                    with open(output_path, 'w') as output:
                        json.dump(result, output, indent=4)
                    logging.info('wrote test results: `%s`', output_path)
                    if video_dir:
                        # A video directory was provided.  Look for a video
                        # corresponding to the same timeline as the test.
                        # Only consider videos that were created within 1
                        # minute of the start of the test.
                        videos = sorted((p for p in
                                         video_dir.expand().files('*.mp4')
                                         if abs(p.ctime - start) < 60),
                                        key=lambda x: -x.ctime)
                        if videos:
                            video = videos[-1]
                            loop.call_soon_threadsafe(update_video, video,
                                                      uuid, output_path)
                except nx.NetworkXNoPath as exception:
                    logging.error('QC test failed: `%s`', exception,
                                  exc_info=True)
                signals.signal('chip-detected').connect(on_chip_detected)

            qc_task = cancellable(_run)
            thread = threading.Thread(target=qc_task)
            thread.daemon = True
            thread.start()

        loop.call_soon_threadsafe(ft.partial(wrapped, sender, **kwargs))

    signals.signal('chip-detected').connect(on_chip_detected)

    thread = threading.Thread(target=chip_video_process,
                              args=(signals, 1280, 720, 0))
    thread.start()

    # Launch window to view chip video.
    loop.run_until_complete(show_chip(signals))

    # Close background thread.
    signals.signal('exit-request').send('main')
    closed.wait()

    #########################

@asyncio.coroutine
def _run_test(signals, proxy, G, way_points, start=None):
    logging.info('Begin DMF chip test routine.')
    G_i = G.copy()
    G_i.remove_node(89)
    G_i.remove_node(30)

    if start is None:
        start = way_points[0]
    way_points_i = np.roll(way_points, -way_points.index(start)).tolist()
    way_points_i += [way_points[0]]

    route = list(it.chain(*[nx.shortest_path(G_i, source, target)[:-1]
                            for source, target in
                            db.dispense
                            .window(way_points_i, 2)])) + [way_points_i[-1]]

    test_route_i = route[:]
    success_nodes = set()

    while len(test_route_i) > 1:
        # Attempt to move liquid from first electrode to second electrode.
        # If liquid movement fails:
        #  * Alert operator (e.g., log notification, alert sound, etc.)
        #  * Attempt to "route around" failed electrode
        source_i = test_route_i.pop(0)

        while test_route_i[0] not in G_i:
            test_route_i.pop(0)
            test_route_i = (nx.shortest_path(G_i, source_i, test_route_i[0])
                            + test_route_i[1:])
        target_i = test_route_i[0]

        for i in range(4):
            try:
                yield asyncio.From(db.dispense
                                   .move_liquid(proxy, [source_i, target_i],
                                                wrapper=ft.partial(asyncio
                                                                   .wait_for,
                                                                   timeout=2)))
                success_nodes.add(target_i)
                break
            except db.dispense.MoveTimeout as exception:
                logging.warning('Timed out moving liquid `%s`->`%s`' %
                                tuple(exception.route_i))
                db.dispense.apply_duty_cycles(proxy, pd.Series(1, index=[source]))
                time.sleep(1.)
        else:
            # Play system "beep" sound to notify user that electrode failed.
            winsound.MessageBeep()
            logging.error('Failed to move liquid to electrode `%s`.', target_i)
            # Remove failed electrode adjacency graph.
            G_i.remove_node(target_i)
            test_route_i = [source_i] + test_route_i
            logging.warning('Attempting to reroute around electrode `%s`.',
                            target_i)
        yield asyncio.From(asyncio.sleep(0))

    db.dispense.apply_duty_cycles(proxy, pd.Series(1, index=test_route_i))
    # Play system "beep" sound to notify user that electrode failed.
    winsound.MessageBeep()
    result = {'route': route, 'failed_nodes': sorted(set(route) -
                                                     success_nodes),
              'success_nodes': sorted(success_nodes)}
    logging.info('Completed - failed electrodes: `%s`' %
                 result['failed_nodes'])
    raise asyncio.Return(result)


def parse_args(args=None):
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(description='DropBot chip quality '
                                     'control')
    parser.add_argument('--video-dir', type=ph.path, help='Directory to search'
                        'for recorded videos matching start time of test.')
    parser.add_argument('-s', '--start', type=int, help='Start electrode')
    parser.add_argument('way_points', help='Test waypoints as JSON list.')

    args = parser.parse_args(args)

    args.way_points = json.loads(args.way_points)

    if args.start is None:
        args.start = args.way_points[0]
    return args


if __name__ == '__main__':
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG,
                        format="[%(asctime)s] %(levelname)s: %(message)s")
    app = QApplication(sys.argv)

    run_test(args.way_points, args.start, args.video_dir)
