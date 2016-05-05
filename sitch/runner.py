""" Starts gpsd and logstash and runs a thread for collecting and enriching
SIM808 engineering mode data.
  One thread for serial interaction and collection
  One thread for enrichment and appending to logfile:
  If log message is GPS, we update the location var for the enrichment
  thread.
"""
from config_helper import ConfigHelper as config_helper
from enricher import Enricher as enricher_module
from utility import Utility as utility
from logger import LogHandler as logger
from sim808 import FonaReader as sim808
import json
import kalibrate
import sys
import threading
import time
from collections import deque
from multiprocessing import Pool


def main():
    global scan_results_queue
    global message_write_queue
    global gps_location
    scan_results_queue = deque([])
    message_write_queue = deque([])
    gps_location = {}
    config = config_helper()
    # Write LS cert
    utility.create_path_if_nonexistent(config.logstash_cert_path)
    utility.write_file(config.logstash_cert_path,
                       config.ls_cert)
    # Write LS config
    utility.write_file("/etc/logstash-forwarder",
                       config.build_logstash_config())
    # Write logrotate config
    utility.write_file("/etc/logrotate.d/sitch",
                       config.build_logrotate_config())
    # Start logstash service
    ls_success = utility.start_component("/etc/init.d/logstash-forwarder start")
    if ls_success is False:
        print "Failed to start logstash-forwarder!!!\nExiting!"
        sys.exit(2)
    # Kill interfering driver
    utility.start_component("modprobe -r dvb_usb_rtl28xxu")
    # Start cron
    cron_success = utility.start_component("/etc/init.d/cron start")
    if cron_success is False:
        print "Failed to start cron, so no logrotate... keep an eye on your disk!"
    # Configure threads
    kalibrate_consumer_thread = threading.Thread(target=kalibrate_consumer,
                                                 args=[config])
    sim808_consumer_thread = threading.Thread(target=sim808_consumer,
                                              args=[config])
    enricher_thread = threading.Thread(target=enricher,
                                       args=[config])
    writer_thread = threading.Thread(target=output,
                                     args=[config])
    kalibrate_consumer_thread.daemon = True
    sim808_consumer_thread.daemon = True
    enricher_thread.daemon = True
    writer_thread.daemon = True
    # Kick off threads
    print "Starting Kalibrate consumer thread..."
    kalibrate_consumer_thread.start()
    print "Starting SIM808 consumer thread..."
    sim808_consumer_thread.start()
    print "Starting enricher thread..."
    enricher_thread.start()
    print "Starting writer thread..."
    writer_thread.start()
    # Periodically check to see if threads are still alive
    while True:
        time.sleep(60)
        if kalibrate_consumer_thread.is_alive is False:
            print "Kalibrate thread died... restarting!"
            kalibrate_consumer_thread.start()
        if sim808_consumer_thread.is_alive is False:
            print "SIM808 consumer thread died... restarting!"
            sim808_consumer_thread.start()
        if enricher_thread.is_alive is False:
            print "Enricher thread died... restarting!"
            enricher_thread.start()
        if writer_thread.is_alive is False:
            print "Writer thread died... restarting!"
            writer_thread.start()
    return


def sim808_consumer(config):
    while True:
        tty_port = config.sim808_port
        band = config.sim808_band
        try:
            consumer = sim808(tty_port)
        except:
            consumer = sim808(tty_port)
        consumer.set_band(band)
        consumer.trigger_gps()
        for line in consumer:
            line["scan_location"]["name"] = config.device_id
            scan_results_queue.append(line)


def kalibrate_consumer(config):
    while True:
        scan_job_template = {"platform": config.platform_name,
                             "scan_results": [],
                             "scan_start": "",
                             "scan_finish": "",
                             "scan_program": "",
                             "scan_location": {}}
        band = config.kal_band
        gain = config.kal_gain
        kal_scanner = kalibrate.Kal("/usr/local/bin/kal")
        start_time = utility.get_now_string()
        kal_results = kal_scanner.scan_band(band, gain=gain)
        end_time = utility.get_now_string()
        scan_document = scan_job_template.copy()
        scan_document["scan_start"] = start_time
        scan_document["scan_finish"] = end_time
        scan_document["scan_results"] = kal_results
        scan_document["scan_program"] = "Kalibrate"
        scan_document["scanner_name"] = config.device_id
        scan_document["scan_location"] = gps_location
        print scan_document
        print "Sending scan to enrichment queue..."
        scan_results_queue.append(scan_document)
    return


def enricher(config):
    """ Enricher breaks apart kalibrate doc into multiple log entries, and
    assembles lines from sim808 into a main doc as well as writing multiple
    lines to the output queue for metadata """
    while True:
        enr = enricher_module(config)
        try:
            scandoc = scan_results_queue.popleft()
            print "Attempting to enrich..."
            doctype = enr.determine_scan_type(scandoc)
            results = []
            if doctype == 'Kalibrate':
                results = enr.enrich_kal_scan(scandoc)
            elif doctype == 'SIM808':
                results = enr.enrich_sim808_scan(scandoc)
            elif doctype == 'GPS':
                results = enr.enrich_gps_scan(scandoc)
                gps_location = scandoc
            message_write_queue.append(results)
            print "Enriched and sent to write queue."
        except IndexError:
            time.sleep(1)


def output(config):
    print "Attempt to instantiate output module..."
    l = logger(config.log_prefix)
    print "Output module instantiated."
    while True:
        try:
            msg_bolus = message_write_queue.popleft()
            print msg_bolus
            msg_type = msg_bolus[0]
            msg = msg_bolus[1]
            if msg is str:
                writemsg = msg
            else:
                writemsg = json.dumps(msg)
            l.write_log_message(msg_type, writemsg)
        except IndexError:
            print "Empty output queue"
            time.sleep(3)

if __name__ == "__main__":
    main()
