#!/usr/bin/env python

"""
Yet Another S3-backed File System, or yas3fs
is a FUSE file system that is designed for speed
caching data locally and using SNS to notify
other nodes for changes that need cache invalidation.
"""

import errno  
import stat  
import time
import os.path
import mimetypes
import sys
import json
import urlparse
import threading
import Queue
import socket
import BaseHTTPServer
import urllib2
import itertools
import M2Crypto
import base64
import logging
import signal
import io
import re
import uuid
import copy

import boto
import boto.s3        
import boto.sns
import boto.sqs
import boto.utils

from sys import argv, exit
from optparse import OptionParser

from boto.s3.key import Key 

from fuse import FUSE, FuseOSError, Operations, LoggingMixIn, fuse_get_context

class Interval():
    """ Simple integer interval arthmetic."""
    def __init__(self):
        self.l = [] # A list of tuples
    def add(self, t):
        assert t[0] <= t[1]
        nl = []
        for i in self.l:
            i0 = i[0] - 1 # To take into account consecutive _integer_ intervals
            i1 = i[1] + 1 # Same as above
            if (i0 <= t[0] and t[0] <= i1) or (i0 <= t[1] and t[1]<= i1) or (t[0] <= i[0] and i[1] <= t[1]):
                t[0] = min(i[0], t[0]) # Enlarge t interval
                t[1] = max(i[1], t[1])
            else:
                nl.append(i)
        nl.append(t)
        self.l = nl
    def contains(self, t):
        assert t[0] <= t[1]
        for i in self.l:
            if (i[0] <= t[0] and t[1] <= i[1]):
                return True
        return False
    def intersects(self, t):
        assert t[0] <= t[1]
        for i in self.l:
            if (i[0] <= t[0] and t[0] <= i[1]) or (i[0] <= t[1] and t[1]<= i[1]) or (t[0] <= i[0] and i[1] <= t[1]):
                return True
        return False

class LinkedListElement():
    """ The element of a linked list."""
    def __init__(self, value, prev=None, next=None):
        self.value = value
        self.prev = prev
        self.next = next
    def delete(self):
        self.prev.next = self.next
        self.next.prev = self.prev
        value = self.value
        del self
        return value

class LinkedList():
    """ A linked list that is used by yas3fs as a LRU index
    for the file system cache."""
    def __init__(self):
        self.head = LinkedListElement(None)
        self.tail = LinkedListElement(None)
        self.head.next = self.tail
        self.tail.prev = self.head
        self.index = {}
        self.lock = threading.RLock()
    def append(self, value):
        with self.lock:
            new = LinkedListElement(value, self.tail.prev, self.tail)
            self.tail.prev.next = new
            self.tail.prev = new
            self.index[value] = new
    def popleft(self):
        with self.lock:
            if self.head.next != self.tail:
        	value = self.head.next.delete()
        	del self.index[value]
        	return value
            else:
        	return None
    def delete(self, value):
        with self.lock:
            if value in self.index:
        	self.index[value].delete()
        	del self.index[value]
                return True
            else:
                return False
    def move_to_the_tail(self, value):
        with self.lock:
            if self.delete(value):
                self.append(value)

class FSData():
    stores = [ 'mem', 'disk' ]
    unknown_store = "Unknown store"
    def __init__(self, cache, store=None, path=None):
        self.cache = cache
        self.store = store
        self.path = path
        self.lock = threading.RLock() # Lock or RLock ???
        self.props = {}
        self.size = 0
        self.etag = None # Something better ???
        if store == 'mem':
            self.content = io.BytesIO()
        elif store == 'disk':
            filename = self.cache.get_cache_filename(self.path)
            if os.path.isfile(filename):
                # There's a file already there
                self.content = io.FileIO(filename, mode='rb+')
                self.update_size(os.stat(filename).st_size)
                self.set('new', None) # Not sure it is the latest version
                # Now search for an etag file
                filename = self.cache.get_cache_etags_filename(self.path)
                if os.path.isfile(filename):
                   with open(filename, 'r') as etag_file:
                        self.etag = etag_file.read()
            else:
                dirname = os.path.dirname(filename)
                try:
                    os.makedirs(dirname)
                except OSError as exc: # Python >2.5
                    if exc.errno == errno.EEXIST and os.path.isdir(dirname):
                        pass
                    else:
                        raise
                self.content = io.FileIO(filename, mode='wb+')
        else:
            raise FSData.unknown_store
    def update_etag(self, new_etag):
        with self.lock:
            if new_etag != self.etag:
                self.etag = new_etag
                if self.store == 'disk':
                    filename = self.cache.get_cache_etags_filename(self.path)
                    dirname = os.path.dirname(filename)
                    try:
                        os.makedirs(dirname)
                    except OSError as exc: # Python >2.5
                        if exc.errno == errno.EEXIST and os.path.isdir(dirname):
                            pass
                        else:
                            raise
                    with open(filename, 'w') as etag_file:
                        etag_file.write(new_etag)
    def update_size(self, delta):
        with self.lock:
            self.size += delta;
        with self.cache.data_size_lock:
            self.cache.size[self.store] += delta
    def get_content_as_string(self):
        if self.store == 'mem':
            with self.lock:
                return self.content.getvalue()
        elif self.store == 'disk':
            with self.lock:
                self.content.seek(0) # Go to the beginning
                return self.content.read()
        else:
            raise FSData.unknown_store
    def has(self, prop):
        with self.lock:
            if prop in self.props:
                return True
            else:
                return False
    def get(self, prop):
        with self.lock:
            if prop in self.props:
                return self.props[prop]
            else:
                return None
    def set(self, prop, value):
        with self.lock:
            self.props[prop] = value
    def delete(self, prop=None):
        with self.lock:
            if prop == None:
                if self.store == 'disk':
                    filename = self.cache.get_cache_filename(self.path)
                    try:
                        os.unlink(filename)
                    except IOError:
                        pass
                    filename = self.cache.get_cache_etags_filename(self.path)
                    try:
                        os.unlink(filename)
                    except IOError:
                        pass
                self.update_size(-self.size)
                range = self.get('range')
                if range:
                    range[2].set() # To make downloading threads go on... and then exit
            elif prop in self.props:
                del self.props[prop]
    def inc(self, prop):
        with self.lock:
            if prop in self.props:
                self.set(prop, self.props[prop] + 1)
            else:
                self.set(prop, 1)
    def dec(self, prop):
        with self.lock:
            if prop in self.props:
                if self.props[prop] > 1:
                    self.set(prop, self.props[prop] - 1)
                else:
                    self.delete(prop)

class FSCache():
    """ File System Cache """
    def __init__(self, cache_path=None):
        self.cache_path = cache_path
        self.lock = threading.RLock()
        self.data_size_lock = threading.Lock()
        self.reset_all()
    def reset_all(self):
         with self.lock:
             self.entries = {} # This will leave disk cache (if any) on place, is this ok???
             self.lru = LinkedList()
             self.size = {}
             for store in FSData.stores:
                self.size[store] = 0
    def get_memory_usage(self):
        return [ len(self.entries) ] + [ self.size[store] for store in FSData.stores ]
    def get_cache_filename(self, path):
        return self.cache_path + '/files' + path # path begins with '/'
    def get_cache_etags_filename(self, path):
        return self.cache_path + '/etags' + path # path begins with '/'
    def add(self, path):
        with self.lock:
            if not self.has(path):
                self.entries[path] = {}
                self.lru.append(path)
    def delete(self, path, prop=None):
        with self.lock:
            if path in self.entries:
        	if prop == None:
        	    for p in self.entries[path].keys():
                        self.delete(path, p)
        	    del self.entries[path]
                    
        	    self.lru.delete(path)
        	else:
        	    if prop in self.entries[path]:
                        if prop == 'data':
                            data = self.entries[path][prop]
                            with data.lock:
                                data.delete() # To clean stuff, e.g. remove cache files
        		del self.entries[path][prop]
    def rename(self, path, new_path):
        with self.lock:
            if path in self.entries:
                self.delete(path, 'key') # Cannot be renamed
                self.delete(new_path) # Assume overwrite
                self.entries[new_path] = self.entries[path]
                self.lru.append(new_path)
                del self.entries[path]
                self.lru.delete(path)
    def get(self, path, prop=None):
        self.lru.move_to_the_tail(path) # Move to the tail of the LRU cache
        try:
            if prop == None:
                return self.entries[path]
            else:
                if prop in self.entries[path]:
                    return self.entries[path][prop]
        except KeyError:
            return None
    def set(self, path, prop, value):
        self.lru.move_to_the_tail(path) # Move to the tail of the LRU cache
        with self.lock:
            if path in self.entries:
        	if prop in self.entries[path]:
                    self.delete(path, prop)
        	self.entries[path][prop] = value
        	return True
            else:
        	return False
    def reset(self, path):
        with self.lock:
            self.delete(path)
            self.add(path)
    def has(self, path, prop=None):
        self.lru.move_to_the_tail(path) # Move to the tail of the LRU cache
        if prop == None:
            if path in self.entries:
                return True
            return False
        else:
            try:
                if prop in self.entries[path]:
                    return True
            except KeyError:
                pass
            return False
    def is_empty(self, path): # A wrapper to improve readability
        return self.has(path) and not self.get(path)
    def is_not_empty(self, path): # A wrapper to improve readability
        return self.has(path) and self.get(path)
 
class SNS_HTTPServer(BaseHTTPServer.HTTPServer):
    """ HTTP Server to receive SNS notifications via HTTP """
    def set_fs(self, fs):
        self.fs = fs

class SNS_HTTPRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """ HTTP Request Handler to receive SNS notifications via HTTP """
    def do_POST(self):
        if self.path != self.server.fs.http_listen_path:
            self.send_response(404)
            return
 
        content_len = int(self.headers.getheader('content-length'))
        post_body = self.rfile.read(content_len)

        message_type = self.headers.getheader('x-amz-sns-message-type')
        message_content = json.loads(post_body)

        # Check SNS signature, I was not able to use boto for this...

        url = message_content['SigningCertURL']
        if not hasattr(self, 'certificate_url') or self.certificate_url != url:
            logger.debug('downloading certificate')
            self.certificate_url = url
            self.certificate = urllib2.urlopen(url).read()
 
        signature_version = message_content['SignatureVersion']
        if signature_version != '1':
            logger.debug('unknown signature version')
            self.send_response(404)
            return

        signature = message_content['Signature']

        del message_content['SigningCertURL']
        del message_content['SignatureVersion']
        del message_content['Signature']
        if 'UnsubscribeURL' in message_content:
            del message_content['UnsubscribeURL']
        string_to_sign = '\n'.join(list(itertools.chain.from_iterable(
                    [ (k, message_content[k]) for k in sorted(message_content.iterkeys()) ]
                    ))) + '\n'

        cert = M2Crypto.X509.load_cert_string(self.certificate)
        pub_key = cert.get_pubkey().get_rsa()
        verify_evp = M2Crypto.EVP.PKey()
        verify_evp.assign_rsa(pub_key)
        verify_evp.reset_context(md='sha1')
        verify_evp.verify_init()
        verify_evp.verify_update(string_to_sign.encode('ascii'))

        if verify_evp.verify_final(signature.decode('base64')):
            self.send_response(200)
            if message_type== 'Notification':
        	changes = message_content['Message']
        	logger.debug('changes = %s' % changes)
                self.server.fs.sync_cache(changes)
            elif message_type == 'SubscriptionConfirmation':
                token = message_content['Token']
                response = self.server.fs.sns.confirm_subscription(self.server.fs.sns_topic_arn, token)
                self.server.fs.http_subscription = response['ConfirmSubscriptionResponse']['ConfirmSubscriptionResult']['SubscriptionArn']
                logger.debug('SNS HTTP subscription = %s' % self.server.fs.http_subscription)
            else:
                logger.debug('unknown message type')
            return
        else:
            logger.debug('wrong signature')

        # If nothing better, return 404
        self.send_response(404)

    def do_GET(self):
        logger.debug('http get')
        self.send_response(404)
    def do_HEAD(self):
        logger.debug('http head')
        self.send_response(404)

class PartOfFSData():
    """ To read just a part of an existing FSData, inspired by FileChunkIO """
    def __init__(self, data, start, length):
        self.data = data
        self.start = start
        self.length = length
        self.pos = 0
    def seek(self, offset, whence=0):
        logger.debug("seek '%i' '%i'" % (offset, whence))
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos = self.pos + offset
        elif whence == 2:
            self.pos = self.length + offset
    def tell(self):
        return self.pos
    def read(self, n=-1):
        logger.debug("read '%i' at '%i' starting from '%i' for '%i'" % (n, self.pos, self.start, self.length))
        if n >= 0:
            n = min([n, self.length - self.pos])
            with self.data.lock:
                self.data.content.seek(self.start + self.pos)
                s = self.data.content.read(n)
            self.pos += len(s)
            return s
        else:
            return self.readall()
    def readall(self):
        return self.read(self.length - self.pos)

class YAS3FS(LoggingMixIn, Operations):
    """ Main FUSE Operations class for fusepy """
    def __init__(self, options):
        # Some constants
        ### self.http_listen_path_length = 30

        # Initialization
        global debug
        debug = options.debug

        self.aws_region = options.region # Not used by S3

        if not options.url:
            errorAndExit("The S3 path to mount in URL format must be provided")
        s3url = urlparse.urlparse(options.url.lower())
        if s3url.scheme != 's3':
            errorAndExit("The S3 path to mount must be in URL format: s3://BUCKET/PATH")
        self.s3_bucket_name = s3url.netloc
        logger.info("S3 bucket: '%s'" % self.s3_bucket_name)
        self.s3_prefix = s3url.path.strip('/')
        logger.info("S3 prefix (can be empty): '%s'" % self.s3_prefix)
        if self.s3_bucket_name == '':
            errorAndExit("The S3 bucket cannot be empty")
        self.sns_topic_arn = options.topic
        if self.sns_topic_arn:
            logger.info("AWS region for SNS/SQS: '" + self.aws_region + "'")
            logger.info("SNS topic ARN: '%s'" % self.sns_topic_arn)
        self.sqs_queue_name = options.queue # must be different for each client
        self.new_queue = options.new_queue
        self.queue_wait_time = int(options.queue_wait_time)
        self.queue_polling_interval = int(options.queue_polling_interval)
        if self.sqs_queue_name:
            logger.info("SQS queue name: '%s'" % self.sqs_queue_name)
        if self.sqs_queue_name or self.new_queue:
            logger.info("SQS queue wait time (in seconds): '%i'" % self.queue_wait_time)
            logger.info("SQS queue polling interval (in seconds): '%i'" % self.queue_polling_interval)
        self.cache_entries = int(options.cache_entries)
        logger.info("Cache entries: '%i'" % self.cache_entries)
        self.cache_mem_size = int(options.cache_mem_size) * (1024 * 1024) # To convert MB to bytes
        logger.info("Cache memory size (in bytes): '%i'" % self.cache_mem_size)
        self.cache_disk_size = int(options.cache_disk_size) * (1024 * 1024) # To convert MB to bytes
        logger.info("Cache disk size (in bytes): '%i'" % self.cache_mem_size)
        self.cache_on_disk = int(options.cache_on_disk) * (1024 * 1024) # To convert MB to bytes
        logger.info("Cache on disk if file size greater than (in bytes): '%i'" % self.cache_on_disk)
        self.cache_check_interval = int(options.cache_check_interval) # seconds
        logger.info("Cache check interval (in seconds): '%i'" % self.cache_check_interval)
        if options.ec2_hostname:
            instance_metadata = boto.utils.get_instance_metadata() # This is very slow (to fail) if used outside of EC2
            self.hostname = instance_metadata['public-hostname']
        else:
            self.hostname = options.hostname
        if self.hostname:
            logger.info("Hostname to listen to SNS HTTP notifications: '%s'" % self.hostname)
        self.sns_http_port = int(options.port or '0')
        if options.port:
            logger.info(" TCP port to listen to SNS HTTP notifications: '%i'" % self.sns_http_port)
        self.buffer_size = int(options.buffer_size) * 1024 # To convert KB to bytes
        logger.info("Download buffer size (in KB, 0 to disable buffering): '%i'" % self.buffer_size)
        self.write_metadata = options.write_metadata
        logger.info("Write metadata (file system attr/xattr) on S3: '%s'" % str(self.write_metadata))
        self.prefetch = options.prefetch
        logger.info("Download prefetch: '%s'" % str(self.prefetch))
        self.multipart_size = int(options.multipart_size) * 1024
        logger.info("Multipart size: '%s'" % str(self.multipart_size))
        self.multipart_num = options.multipart_num
        logger.info("Multipart maximum number of parallel threads: '%s'" % str(self.multipart_num))
        self.multipart_retries = options.multipart_retries
        logger.info("Multipart maximum number of retries per part: '%s'" % str(self.multipart_retries))

        # Internal Initialization
        if options.cache_path == '':
            cache_path = '/tmp/yas3fs/' + self.s3_bucket_name + '/' + self.s3_prefix
        else:
            cache_path = options.cache_path
        logger.info("Cache path (on disk): '%s'" % cache_path)
        self.cache = FSCache(cache_path)
        self.publish_queue = Queue.Queue()

        # AWS Initialization
        try:
            self.s3 = boto.connect_s3() # Not using AWS region for S3, got an error otherwise, depending on the bucket
        except boto.exception.NoAuthHandlerFound:
            errorAndExit("no AWS credentials found")
        if not self.s3:
            errorAndExit("no S3 connection")
        try:
            self.s3_bucket = self.s3.get_bucket(self.s3_bucket_name)
        except boto.exception.S3ResponseError:
            errorAndExit("S3 bucket not found")

        pattern = re.compile('[\W_]+') # Alphanumeric characters only, to be used for pattern.sub('', s)

        unique_id_list = []
        if options.id:
            unique_id_list.append(options.id)
        unique_id_list.append(str(uuid.uuid1()))
        self.unique_id = '-'.join(pattern.sub('', s) for s in unique_id_list)
        logger.info("Unique node ID: '%s'" % self.unique_id)
                
        if self.sns_topic_arn:
            if not self.aws_region in (r.name for r in boto.sns.regions()):
                errorAndExit("wrong AWS region '%s' for SNS" % self.aws_region)
            self.sns = boto.sns.connect_to_region(self.aws_region)
            if not self.sns:
                errorAndExit("no SNS connection")
            try:
                topic_attributes = self.sns.get_topic_attributes(self.sns_topic_arn)
            except boto.exception.BotoServerError:
                errorAndExit("SNS topic ARN not found in region '%s' " % self.aws_region)
            if not self.sqs_queue_name and not self.new_queue:
                if not (self.hostname and self.sns_http_port):
                    errorAndExit("With and SNS topic either the SQS queue name or the hostname and port to listen to SNS HTTP notifications must be provided")

        if self.sqs_queue_name or self.new_queue:
            if not self.sns_topic_arn:
                errorAndExit("The SNS topic must be provided when an SQS queue is used")
            if not self.aws_region in (r.name for r in boto.sqs.regions()):
                errorAndExit("wrong AWS region '" + self.aws_region + "' for SQS")
            self.sqs = boto.sqs.connect_to_region(self.aws_region)
            if not self.sqs:
                errorAndExit("no SQS connection")
            if self.new_queue:
                self.sqs_queue_name = '-'.join([ 'yas3fs',
                                               pattern.sub('', self.s3_bucket_name),
                                               pattern.sub('', self.s3_prefix),
                                               self.unique_id ])
                self.queue = None
            else:
                self.queue =  self.sqs.lookup(self.sqs_queue_name)
            if not self.queue:
                self.queue = self.sqs.create_queue(self.sqs_queue_name)
            logger.info("SQS queue name (new): '%s'" % self.sqs_queue_name)
            self.queue.set_message_class(boto.sqs.message.RawMessage) # There is a bug with the default Message class in boto

        if self.hostname or self.sns_http_port:
            if not self.sns_topic_arn:
                errorAndExit("The SNS topic must be provided when the hostname/port to listen to SNS HTTP notifications is given")            

        if self.sns_http_port:
            if not self.hostname:
                errorAndExit("The hostname must be provided with the port to listen to SNS HTTP notifications")
            ### self.http_listen_path = '/sns/' + base64.urlsafe_b64encode(os.urandom(self.http_listen_path_length))
            self.http_listen_path = '/sns'
            self.http_listen_url = "http://%s:%i%s" % (self.hostname, self.sns_http_port, self.http_listen_path)

        if self.multipart_size < 5242880:
            errorAndExit("The minimum size for multipart upload supported by S3 is 5MB")
        if self.multipart_retries < 1:
            errorAndExit("The number of retries for multipart uploads cannot be less than 1")

        signal.signal(signal.SIGINT, self.handler)

    def init(self, path):
        logger.debug("init '%s'" % (path))
        self.publish_thread = threading.Thread(target=self.publish_changes)
        self.publish_thread.daemon = True
        self.publish_thread.start()

        if self.sqs_queue_name:
            self.queue_listen_thread = threading.Thread(target=self.listen_for_changes_over_sqs)
            self.queue_listen_thread.daemon = True
            self.queue_listen_thread.start()
            logger.debug("Subscribing '%s' to '%s'" % (self.sqs_queue_name, self.sns_topic_arn))
            response = self.sns.subscribe_sqs_queue(self.sns_topic_arn, self.queue)
            self.sqs_subscription = response['SubscribeResponse']['SubscribeResult']['SubscriptionArn']
            logger.debug('SNS SQS subscription = %s' % self.sqs_subscription)
        else:
            self.queue_listen_thread = None

        if self.sns_http_port:
            self.http_listen_thread = threading.Thread(target=self.listen_for_changes_over_http)
            self.http_listen_thread.daemon = True
            self.http_listen_thread.start()
            self.sns.subscribe(self.sns_topic_arn, 'http', self.http_listen_url)
        else:
            self.http_listen_thread = None

        self.check_cache_thread = threading.Thread(target=self.check_cache_size)
        self.check_cache_thread.daemon = True
        self.check_cache_thread.start()

    def handler(signum, frame):
        self.destroy('/')

    def flush_all_cache(self):
        logger.debug("flush_all_cache")
        with self.cache.lock:
            for path in self.cache.entries:
                data = self.cache.get(path, 'data')
                if data and data.has('change'):
                    self.flush(path)
 
    def destroy(self, path):
        logger.debug("destroy '%s'" % (path))
        # Cleanup for unmount
        logger.info('file system unmount')

        if self.http_listen_thread:
            self.httpd.shutdown() # To stop HTTP listen thread
            logger.debug("waiting for HTTP listen thread to shutdown...")
            self.http_listen_thread.join(5.0) # 5 seconds should be enough   
            logger.debug("HTTP listen thread ended")
            self.sns.unsubscribe(self.http_subscription)
            logger.debug("Unsubscribed SNS HTTP endpoint")
        if self.queue_listen_thread:
            self.sqs_queue_name = None # To stop queue listen thread
            logger.debug("waiting for SQS listen thread to shutdown...")
            self.queue_listen_thread.join(self.queue_wait_time + 1.0)
            logger.debug("SQS listen thread ended")
            self.sns.unsubscribe(self.sqs_subscription)
            logger.debug("Unsubscribed SNS SQS endpoint")
            if self.new_queue:
                self.sqs.delete_queue(self.queue, force_deletion=True)
                logger.debug("New queue deleted")

        self.flush_all_cache()

        if self.sns_topic_arn:
            while not self.publish_queue.empty():
                time.sleep(1.0)
            self.sns_topic_arn = None # To stop publish thread
            logger.debug("waiting for SNS publish thread to shutdown...")
            self.publish_thread.join(2.0) # 2 seconds should be enough
        if  self.cache_entries:
            self.cache_entries = 0 # To stop memory thread
            logger.debug("waiting for check cache thread to shutdown...")
            self.check_cache_thread.join(self.cache_check_interval + 1.0)
        
    def listen_for_changes_over_http(self):
        logger.info("Listening on: '%s'" % self.http_listen_rl)
        server_class = SNS_HTTPServer
        handler_class = SNS_HTTPRequestHandler
        server_address = ('', self.sns_http_port)
        self.httpd = server_class(server_address, handler_class)
        self.httpd.set_fs(self)
        self.httpd.serve_forever()

    def listen_for_changes_over_sqs(self):
        logger.info("Listening on queue: '%s'" % self.queue.name)
        while self.sqs_queue_name:
            if self.queue_wait_time > 0:
                # Using SQS long polling, needs boto >= 2.7.0
                messages = self.queue.get_messages(10, wait_time_seconds=self.queue_wait_time)
            else:
                messages = self.queue.get_messages(10)
            logger.debug("Got %i messages from SQS" % len(messages))
            if messages:
                for m in messages:
                    content = json.loads(m.get_body())
                    changes = content['Message'].encode('ascii')
                    self.sync_cache(changes)
                    m.delete()
            else:
                if self.queue_polling_interval > 0:
                    time.sleep(self.queue_polling_interval)

    def invalidate_cache(self, path, etag=None):
        logger.debug("invalidate_cache '%s' '%s'" % (path, etag))
        with self.cache.lock:
            self.cache.delete(path, 'key')
            self.cache.delete(path, 'attr')
            self.cache.delete(path, 'xattr')
            if self.cache.has(path, 'data'):
                if self.cache.get(path, 'data').has('range'):
                    self.cache.delete(path, 'data')
                else:
                    self.cache.get(path, 'data').set('new', etag)
            if self.cache.is_empty(path):
                self.cache.delete(path)
                self.reset_parent_readdir(path)

    def delete_cache(self, path):
        logger.debug("delete_cache '%s'" % (path))
        with self.cache.lock:
            self.cache.delete(path)
            self.reset_parent_readdir(path)

    def sync_cache(self, changes):
        logger.debug("sync_cache '%s'" % (changes))
        c = json.loads(changes)
        if not c[0] == self.unique_id: # discard message coming from itself
            if c[1] in ( 'mkdir', 'mknod', 'symlink' ) and c[2] != None:
                self.delete_cache(c[2])
            elif c[1] in ( 'rmdir', 'unlink' ) and c[2] != None:
                self.delete_cache(c[2])
            elif c[1] == 'rename' and c[2] != None and c[3] != None:
                self.delete_cache(c[2])
                self.delete_cache(c[3])
            elif c[1] == 'flush':
                if c[2] != None:
                    self.invalidate_cache(c[2], c[3])
                else: # Invalidate all the cached data
                    for path in self.cache.entries:
                        self.invalidate_cache(path)
            elif c[1] == 'md':
                if c[2]:
                    self.cache.delete(c[3], 'key')
                    self.cache.delete(c[3], c[2])
            elif c[1] == 'reset':
                with self.cache.lock:
                    self.flush_all_cache()
                    self.cache.reset_all() # Completely reset the cache
            elif c[1] == 'url':
                with self.cache.lock:
                    self.flush_all_cache()
                    self.cache.reset_all() # Completely reset the cache
                    s3url = urlparse.urlparse(c[2])
                    if s3url.scheme != 's3':
                        errorAndExit("The S3 path to mount must be in URL format: s3://BUCKET/PATH")
                    self.s3_bucket_name = s3url.netloc
                    logger.info("S3 bucket: '%s'" % self.s3_bucket_name)
                    self.s3_prefix = s3url.path.strip('/')
                    logger.info("S3 prefix: '%s'" % self.s3_prefix)
                    try:
                        self.s3_bucket = self.s3.get_bucket(self.s3_bucket_name)
                    except boto.exception.S3ResponseError:
                        errorAndExit("S3 bucket not found")
            elif c[1] == 'cache':
                if c[2] == 'entries' and c[3] > 0:
                    self.cache_entries = int(c[3])
                elif c[2] == 'mem' and c[3] > 0:
                    self.cache_mem_size = int(c[3]) * (1024 * 1024) # MB
                elif c[2] == 'disk' and c[3] > 0:
                    self.cache_disk_size = int(c[3]) * (1024 * 1024) # MB
            elif c[1] == 'buffer' and c[3] >= 0:
                if c[2] == 'size':
                    self.buffer_size = int(c[3]) * 1024 # KB
            elif c[1] == 'prefetch':
                if c[2] == 'on':
                    self.prefetch = True
                elif c[2] == 'off':
                    self.prefetch = False
            elif c[1] == 'multipart':
                if c[2] == 'size' and c[3] >= 5120:
                    self.multipart_size = c[3] * 1024
                elif c[2] == 'num' and c[3] >= 0:
                    self.multipart_num = c[3]
                elif c[2] == 'retries' and c[3] >= 1:
                    self.multipart_retries = c[3]

    def publish_changes(self):
        while self.sns_topic_arn:
            try:
                message = self.publish_queue.get(True, 1) # 1 second time-out
                message.insert(0, self.unique_id)
                full_message = json.dumps(message)
                self.sns.publish(self.sns_topic_arn, full_message.encode('ascii'))
                self.publish_queue.task_done()
            except Queue.Empty:
                pass
                
    def publish(self, message):
        logger.debug("publish '%s'" % (message))
        self.publish_queue.put(message)

    def check_cache_size(self):
        logger.debug("check_cache_size")
        while self.cache_entries:
            num_entries, mem_size, disk_size = self.cache.get_memory_usage()
            logger.debug("num_entries, mem_size, disk_size: %i, %i, %i" % (num_entries, mem_size, disk_size))
            purge = False
            if num_entries > self.cache_entries:
                with self.cache.lock:
                    path = self.cache.lru.popleft()
                    logger.debug("purge: %s ?" % path)
                    data = self.cache.has(path, 'data')
                    if data and (data.has('open') or data.has('change')):
                        self.cache.lru.append(path)
                    else:
                        logger.debug("purge: yes")
                        self.cache.delete(path)
                        purge = True
            if mem_size > self.cache_mem_size:
                with self.cache.lock:
                    path = self.cache.lru.popleft()
                    data = self.cache.get(path, 'data')
                    if data and (data.store != 'mem' or data.has('open') or data.has('change')):
                        self.cache.lru.append(path)
                    else:
                        self.cache.delete(path)
                        purge = True
            if disk_size > self.cache_disk_size:
                with self.cache.lock:
                    path = self.cache.lru.popleft()
                    data = self.cache.get(path, 'data')
                    if data and (data.store != 'disk' or data.has('open') or data.has('change')):
                        self.cache.lru.append(path)
                    else:
                        self.cache.delete(path)
                        purge = True
            if not purge:
                time.sleep(self.cache_check_interval)

    def add_to_parent_readdir(self, path):
        logger.debug("add_to_parent_readdir '%s'" % (path))
        (parent_path, dir) = os.path.split(path)
        logger.debug("parent_path '%s'" % (parent_path))
        with self.cache.lock:
            dirs = self.cache.get(parent_path, 'readdir')
            if dirs != None and dirs.count(dir) == 0:
                dirs.append(dir)

    def remove_from_parent_readdir(self, path):
        logger.debug("remove_from_parent_readdir '%s'" % (path))
        (parent_path, dir) = os.path.split(path)
        logger.debug("parent_path '%s'" % (parent_path))
        with self.cache.lock:
            dirs = self.cache.get(parent_path, 'readdir')
            if dirs != None and dirs.count(dir) > 0:
                dirs.remove(dir)

    def reset_parent_readdir(self, path):
        logger.debug("reset_parent_readdir '%s'" % (path))
        (parent_path, dir) = os.path.split(path)
        logger.debug("parent_path '%s'" % (parent_path))
        self.cache.delete(parent_path, 'readdir')

    def join_prefix(self, path):
        if self.s3_prefix == '':
            return path[1:] # Remove beginning "/"
        else:
            return self.s3_prefix + path

    def get_key(self, path):
        key = self.cache.get(path, 'key')
        if key:
            return key
        key = self.s3_bucket.get_key(self.join_prefix(path))
        if not key:
            key = self.s3_bucket.get_key(self.join_prefix(path + '/'))
        if key:
            self.cache.set(path, 'key', key)
        return key

    def get_metadata(self, path, metadata_name, key=None):
        logger.debug("get_metadata -> '%s' '%s' '%s'" % (path, metadata_name, key))
        if not self.cache.has(path, metadata_name):
            if not key:
                key = self.get_key(path)
            if not key:
                if path == '/': # First time mount of a new file system
                    self.cache.delete(path)
                    self.mkdir('', 0755)
                    self.cache.rename('', path)
                    return self.cache.get(path, metadata_name)
                else:
                    full_path = self.join_prefix(path + '/')
                    key_list = self.s3_bucket.list(full_path) # Don't need to set a delimeter here
                    if len(list(key_list)) == 0:
                        self.cache.add(path) # It is empty to cache further checks
                        logger.debug("get_metadata '%s' '%s' '%s' return None" % (path, metadata_name, key))
                        return None
###                     raise FuseOSError(errno.ENOENT)
            metadata_values = {}
            if key:
                s = key.get_metadata(metadata_name)
            else:
                s = None
            if metadata_name == 'attr': # Custom exception(s)
                if key:
                    metadata_values['st_size'] = str(key.size)
                else:
                    metadata_values['st_size'] = '0'                
                if not s: # Set default attr to browse any S3 bucket TODO directories
		    uid, gid, pid = fuse_get_context()
 		    metadata_values['st_uid'] = str(int(uid))
 		    metadata_values['st_gid'] = str(int(gid))
                    if key and key.name != '' and key.name[-1] != '/':
                        metadata_values['st_mode'] = str(stat.S_IFREG | 0755)
                    else:
                        metadata_values['st_mode'] = str(stat.S_IFDIR | 0755)
                    if key and key.last_modified:
                        now = str(time.mktime(time.strptime(key.last_modified, "%a, %d %b %Y %H:%M:%S %Z")))
                    else:
                        now = str(time.time()) # Do something better ??? 
                    metadata_values['st_mtime'] = now
                    metadata_values['st_atime'] = now
                    metadata_values['st_ctime'] = now
	    if s:
		for kv in s.split(';'):
		    k, v = kv.split('=')
		    metadata_values[k] = v
	    self.cache.add(path)
	    self.cache.set(path, metadata_name, metadata_values)
        else:
            metadata_values = self.cache.get(path, metadata_name)
        logger.debug("get_metadata <- '%s' '%s' '%s' '%s'" % (path, metadata_name, key, metadata_values))
	return metadata_values

    def set_metadata(self, path, metadata_name, metadata_values, key=None):
        logger.debug("set_metadata '%s' '%s' '%s' '%s'" % (path, metadata_name, metadata_values, key))
	self.cache.set(path, metadata_name, metadata_values)
        data = self.cache.get(path, 'data')
        if self.write_metadata and (key or (data and not data.has('change'))): # No change in progress, I should write now
	    if not key:
                key = self.get_key(path)
	    if key:
		if metadata_values:
		    s = ';'.join(['%s=%s' % (k,v) for k,v in metadata_values.iteritems()
				  if not (metadata_name == 'attr' and k == 'st_size')])
		    key.metadata[metadata_name] = s
		elif metadata_name in key.metadata:
		    del key.metadata[metadata_name]
                if data and not data.has('change'):
                    logger.debug("writing metadata '%s' '%s' '%s' '%s'" % (path, metadata_name, metadata_values, key))
                    md = key.metadata
                    md['Content-Type'] = key.content_type # Otherwise we loose the Content-Type with Copy
                    key.copy(key.bucket.name, key.name, md, preserve_acl=False) # Do I need to preserve ACL?
                    self.publish(['md', metadata_name, path])

    def getattr(self, path, fh=None):
        logger.debug("getattr -> '%s' '%s'" % (path, fh))
        if self.cache.is_empty(path):
            logger.debug("getattr <- '%s' '%s' ENOENT" % (path, fh))
            raise FuseOSError(errno.ENOENT)
	attr = self.get_metadata(path, 'attr')
        if attr == None:
            logger.debug("getattr <- '%s' '%s' ENOENT" % (path, fh))
            raise FuseOSError(errno.ENOENT)
	st = {}
	st['st_mode'] = int(attr['st_mode'])
	st['st_atime'] = float(attr['st_atime']) # Should I update this ???
	st['st_mtime'] = float(attr['st_mtime']) # Should I use k.last_modified ???
	st['st_ctime'] = float(attr['st_ctime'])
        st['st_uid'] = int(attr['st_uid'])
        st['st_gid'] = int(attr['st_gid'])
	st['st_size'] = int(attr['st_size'])
        if stat.S_ISDIR(st['st_mode']) and st['st_size'] == 0:
            st['st_size'] = 4096 # For compatibility...
	st['st_nlink'] = 1 # Something better TODO ???
        if self.prefetch: # Prefetch
            if stat.S_ISDIR(st['st_mode']):
                self.readdir(path)
            else:
                self.check_data(path)
        logger.debug("getattr <- '%s' '%s' '%s'" % (path, fh, st))
        return st

    def readdir(self, path, fh=None):
        logger.debug("readdir '%s' '%s'" % (path, fh))

	if self.cache.is_empty(path):
            logger.debug("readdir '%s' '%s' ENOENT" % (path, fh))
	    raise FuseOSError(errno.ENOENT)

	self.cache.add(path)

        dirs = self.cache.get(path, 'readdir')

	if not dirs:
	    full_path = self.join_prefix(path)
            if full_path != '' and full_path[-1] != '/':
                full_path += '/'
	    key_list = self.s3_bucket.list(full_path, '/')
	    dirs = ['.', '..']
	    for k in key_list:
		d = k.name.encode('ascii')[len(full_path):]
		if len(d) > 0:
		    if d[-1] == '/':
			d = d[:-1]
		    dirs.append(d)
	    self.cache.set(path, 'readdir', dirs)

	return dirs

    def mkdir(self, path, mode):
        logger.debug("mkdir '%s' '%s'" % (path, mode))
	if self.cache.is_not_empty(path):
	    raise FuseOSError(errno.EEXIST)
	k = self.get_key(path)
	if k:
	    raise FuseOSError(errno.EEXIST)
	now = str(time.time())
	uid, gid, pid = fuse_get_context()
	attr = {}
	attr['st_uid'] = str(int(uid))
	attr['st_gid'] = str(int(gid))
	attr['st_atime'] = now
	attr['st_mtime'] = now
	attr['st_ctime'] = now
	attr['st_size'] = '0'
	attr['st_mode'] = str(int(stat.S_IFDIR | mode))
	self.cache.delete(path)
	self.cache.add(path)
        data = FSData(self.cache, 'mem')
        self.cache.set(path, 'data', data)
        data.set('change', True)
	k = Key(self.s3_bucket)
	self.set_metadata(path, 'attr', attr, k)
	self.set_metadata(path, 'xattr', {}, k)
	k.key = self.join_prefix(path + '/')
	k.set_contents_from_string('', headers={'Content-Type': 'application/x-directory'})
        self.cache.set(path, 'key', k)
	data.delete('change')
	self.cache.set(path, 'readdir', ['.', '..']) # the directory is empty
	if path != '':
            self.add_to_parent_readdir(path)
            self.publish(['mkdir', path])
	return 0
 
    def symlink(self, path, link):
        logger.debug("symlink '%s' '%s'" % (path, link))
	if self.cache.is_not_empty(path):
	    raise FuseOSError(errno.EEXIST)
	k = self.get_key(path)
	if k:
	    raise FuseOSError(errno.EEXIST)
	now = str(time.time())
	uid, gid, pid = fuse_get_context()
	attr = {}
	attr['st_uid'] = str(int(uid))
	attr['st_gid'] = str(int(gid))
	attr['st_ctime'] = now # atime, mtime and size are updated in the following 'write'
	attr['st_mode'] = str(stat.S_IFLNK | 0755)
	self.cache.delete(path)
	self.cache.add(path)
        data = FSData(self.cache, 'mem')
        self.cache.set(path, 'data', data)
        self.write(path, link, 0)
	k = Key(self.s3_bucket)
	self.set_metadata(path, 'attr', attr, k)
	self.set_metadata(path, 'xattr', {}, k)
	k.key = self.join_prefix(path)
	k.set_contents_from_string(link, headers={'Content-Type': 'application/x-symlink'})
        self.cache.set(path, 'key', k)
	data.delete('change')
	self.add_to_parent_readdir(path)
	self.publish(['symlink', path])
	return 0

    def check_data(self, path): 
        logger.debug("check_data '%s'" % (path))
        data = self.cache.get(path, 'data')
        if not data or data.has('new'):
            k = self.get_key(path)
            if not k:
		return False
            if not data:
                if k.size < self.cache_on_disk:
                    data = FSData(self.cache, 'mem')
                else:
                    data = FSData(self.cache, 'disk', path)
                self.cache.set(path, 'data', data)
            new_etag = data.get('new')
            etag = k.etag[1:-1]
            if not new_etag or new_etag == etag:
                data.delete('new')
            else: # I'm not sure I got the latest version
                self.cache.delete(path, 'key')
                data.set('new', None) # Next time don't check the Etag
            if data.etag == etag:
                return True
            data.update_size(-data.size) # Can be zero...
            if self.buffer_size > 0:
                with data.lock:
                    if data.has('range'):
                        return True
                    interval = Interval()
                    next_interval = Interval()
                    next_interval.add([0, self.buffer_size])
                    data.set('range', (interval, next_interval, threading.Event()))
                t = threading.Thread(target=self.download_data, args=(path, 0))
                t.daemon = True
                t.start()
            else:
                k.get_contents_to_file(data)
                data.update_size(k.size)
                data.update_etag(k.etag[1:-1])
	return True

    def download_data(self, path, starting_from):
        logger.debug("download_data '%s' %i [thread '%s']" % (path, starting_from, threading.current_thread().name))

        data = self.cache.get(path, 'data')
        key = copy.deepcopy(self.get_key(path)) # Something better ??? I need a local copy ok the key...

        delete_flag = False

        try:
            range = [ starting_from, key.size ]
            range_headers = { 'Range' : 'bytes=' + str(range[0]) + '-' + str(range[1]) }
            key.open_read(headers=range_headers)
            pos = range[0]
            while True:
                with self.cache.lock:
                    if data.has('range'):
                        (interval, next_interval, event) = data.get('range')
                    else:
                        return
                    new_interval = [pos, pos + self.buffer_size - 1]
                    next_interval.add(new_interval)
                    self.cache.set(interval, next_interval, event)
                bytes = key.resp.read(self.buffer_size)
                if not bytes:
                    key.close()
                    break
                with self.cache.lock:
                    if data.has('range'):
                        (interval, next_interval, event) = data.get('range')
                    else:
                        return
                    with data.lock:
                        data.content.seek(pos)
                        data.content.write(bytes)
                    length = len(bytes)
                    new_interval = [pos, pos + length - 1]
                    pos += length
                    overlap = interval.intersects(new_interval)
                    if overlap:
                        logger.debug("download_data overlap '%s' for '%s' [thread '%s']" %
                                     (overlap, new_interval, threading.current_thread().name))
                    interval.add(new_interval)
                    data.update_size(length) # Should I use max from interval ???
                    data.set('range', (interval, next_interval, threading.Event()))
                    event.set()
                    if overlap or pos >= key.size: # Check also if pos >= key.size ???
                        key.close()
                        break
        except boto.exception.S3ResponseError:
            delete_flag = True

        logger.debug("download_data end '%s' %i-%i [thread '%s']" % (path, starting_from, pos, threading.current_thread().name))

        with self.cache.lock:
            if data.has('range'):
                (interval, next_interval, event) = data.get('range')
                if interval.contains([0, key.size - 1]): # -1 ???
                    data.delete('range')
                    data.update_etag(key.etag[1:-1])
                    logger.debug("download_data all ended '%s' [thread '%s']" % (path, threading.current_thread().name))
                    event.set()

        if delete_flag:
            self.cache.delete(path) # Something went wrong...

    def readlink(self, path):
        logger.debug("readlink '%s'" % (path))
	if self.cache.is_empty(path):
            logger.debug("readlink '%s' ENONENT" % (path))
	    raise FuseOSError(errno.ENOENT)
	self.cache.add(path)
	if stat.S_ISLNK(self.getattr(path)['st_mode']):
	    if not self.check_data(path):
                logger.debug("readlink '%s' ENONENT" % (path))
		raise FuseOSError(errno.ENOENT)
            while True:
                data = self.cache.get(path, 'data')
                if data == None:
                    raise FuseOSError(errno.ENOENT) # ??? That should not happen
                data_range = data.get('range')
                if data_range == None:
                    break
                data_range[2].wait()
	    return data.get_content_as_string()
        logger.debug("readlink '%s' EINVAL" % (path))
	raise FuseOSError(errno.EINVAL)
 
    def rmdir(self, path):
        logger.debug("rmdir '%s'" % (path))
	if self.cache.is_empty(path):
            logger.debug("rmdir '%s' ENOENT" % (path))
	    raise FuseOSError(errno.ENOENT)
	k = self.get_key(path) # Should I use cache here ???
	if not k:
            logger.debug("rmdir '%s' ENOENT" % (path))
	    raise FuseOSError(errno.ENOENT)
	full_path = self.join_prefix(path + '/')
	key_list = self.s3_bucket.list(full_path) # Don't need to set a delimeter here
	for l in key_list:
	    if l.name != full_path:
                logger.debug("rmdir '%s' ENOTEMPTY" % (path))
		raise FuseOSError(errno.ENOTEMPTY)
	k.delete()
	self.cache.reset(path) # Cache invaliation
	self.remove_from_parent_readdir(path)
	self.publish(['rmdir', path])
	return 0

    def truncate(self, path, size):
        logger.debug("truncate '%s' '%i'" % (path, size))
	if self.cache.is_empty(path):
            logger.debug("truncate '%s' '%i' ENOENT" % (path, size))
	    raise FuseOSError(errno.ENOENT)
	self.cache.add(path)
	if not self.check_data(path):
            logger.debug("truncate '%s' '%i' ENOENT" % (path, size))
	    raise FuseOSError(errno.ENOENT)
        while True:
            data = self.cache.get(path, 'data')
            if data == None:
                raise FuseOSError(errno.ENOENT) # ??? That should not happen
            data_range = data.get('range')
            if data_range == None:
                break
            if data_range[0].contains([0, size]):
                data.delete('range')
                # Release event (set) ???
                break
            data_range[2].wait()
        data.content.truncate(size)
        now = str(time.time())
	attr = self.get_metadata(path, 'attr')
        old_size = int(attr['st_size'])
	data.set('change', True)
        if size != old_size:
            attr['st_size'] = str(size)
            data.update_size(size - old_size)
        attr['st_mtime'] = now
        attr['st_atime'] = now
        self.set_metadata(path, 'attr', attr)
	return 0

    ### Should work for files in cache but not flushed to S3...
    def rename(self, path, new_path):
        logger.debug("rename '%s' '%s'" % (path, new_path))
        if self.cache.is_empty(path):
            logger.debug("rename '%s' '%s' ENOENT" % (path, new_path))
            raise FuseOSError(errno.ENOENT)
        key = self.get_key(path)
        if not key and not self.cache.has(path):
            logger.debug("rename '%s' '%s' ENOENT" % (path, new_path))
            raise FuseOSError(errno.ENOENT)
        new_parent_key = self.get_key(os.path.dirname(new_path))
        if not new_parent_key:
            logger.debug("rename '%s' '%s' ENOENT" % (path, new_path))
            raise FuseOSError(errno.ENOENT)
        to_copy = {}
        if key:
            if key.name[-1] == '/':
                key_list = self.s3_bucket.list(key.name)
                for k in key_list:
                    source = k.name.encode('ascii')
                    target = self.join_prefix(new_path + source[len(key.name) - 1:])
                    to_copy[source] = target
            else:
                to_copy[key.name] = self.join_prefix(new_path)
        else:
            ### Should I manage a "full" search in cache for files in path ???
            to_copy[self.join_prefix(path)] = self.join_prefix(new_path) # For files in cache but still not flushed to S3, doesn't work for dirs!!!
        for source, target in to_copy.iteritems():
            source_path = source[len(self.s3_prefix):].rstrip('/')
            if source_path[0] != '/':
                source_path = '/' + source_path
            target_path = target[len(self.s3_prefix):].rstrip('/')
            if target_path[0] != '/':
                target_path = '/' + target_path
            self.cache.rename(source_path, target_path)
            key = self.s3_bucket.get_key(source)
            if key: # For files in cache but still not flushed to S3
                md = key.metadata
                md['Content-Type'] = key.content_type # Otherwise we loose the Content-Type with S3 Copy
                key.copy(key.bucket.name, target, md, preserve_acl=False) # Do I need to preserve ACL?
                key.delete()
            self.publish(['rename', source_path, target_path])
        self.remove_from_parent_readdir(path)
        self.add_to_parent_readdir(new_path)

    def mknod(self, path, mode, dev=None):
        logger.debug("mknod '%s' '%i' '%s'" % (path, mode, dev))
        if self.cache.is_not_empty(file):
            logger.debug("mknod '%s' '%i' '%s' EEXIST" % (path, mode, dev))
            raise FuseOSError(errno.EEXIST)
	else:
	    k = self.get_key(path)
	    if k:
                logger.debug("mknod '%s' '%i' '%s' EEXIST" % (path, mode, dev))
		raise FuseOSError(errno.EEXIST)
	    self.cache.add(path)
	now = str(time.time())
	uid, gid, pid = fuse_get_context()
	attr = {}
	attr['st_uid'] = str(int(uid))
	attr['st_gid'] = str(int(gid))
	attr['st_mode'] = str(stat.S_IFREG | mode)
	attr['st_atime'] = now
	attr['st_mtime'] = now
	attr['st_ctime'] = now
	attr['st_size'] = '0' # New file
        if self.cache_on_disk > 0:
            data = FSData(self.cache, 'mem') # New files (almost) always cache in mem - is it ok ???
        else:
            data = FSData(self.cache, 'disk', path)
	self.cache.set(path, 'data', data)
	data.set('change', True)
	self.set_metadata(path, 'attr', attr)
	self.set_metadata(path, 'xattr', {})
	self.add_to_parent_readdir(path)
	self.publish(['mknod', path])
	return 0

    def unlink(self, path):
        logger.debug("unlink '%s'" % (path))
	if self.cache.is_empty(path):
            logger.debug("unlink '%s' ENOENT" % (path))
	    raise FuseOSError(errno.ENOENT)
	k = self.get_key(path)
	if not k and not self.cache.has(path):
            logger.debug("unlink '%s' ENOENT" % (path))
	    raise FuseOSError(errno.ENOENT)
        if k:
            k.delete()
	self.cache.reset(path)
	self.remove_from_parent_readdir(path)
	self.publish(['unlink', path])
	return 0

    def create(self, path, mode, fi=None):
        logger.debug("create '%s' '%i' '%s'" % (path, mode, fi))
	return self.open(path, mode)

    def open(self, path, flags):
        logger.debug("open '%s' '%i'" % (path, flags))
	self.cache.add(path)
	if not self.check_data(path):
	    self.mknod(path, flags)
	self.cache.get(path, 'data').inc('open')
        logger.debug("open '%s' '%i' '%s'" % (path, flags, self.cache.get(path, 'data').get('open')))
	return 0

    def release(self, path, flags):
        logger.debug("release '%s' '%i'" % (path, flags))
        if self.cache.is_empty(path):
            logger.debug("release '%s' '%i' ENOENT" % (path, flags))
            raise FuseOSError(errno.ENOENT)
        data = self.cache.get(path, 'data')
        if data:
            data.dec('open')
        logger.debug("release '%s' '%i' '%s'" % (path, flags, data.get('open')))
	return 0

    def read(self, path, length, offset, fh=None):
        logger.debug("read '%s' '%i' '%i' '%s'" % (path, length, offset, fh))
        if not self.cache.has(path) or self.cache.is_empty(path):
            logger.debug("read '%s' '%i' '%i' '%s' ENOENT" % (path, length, offset, fh))
            raise FuseOSError(errno.ENOENT)
        while True:
            data_range = self.cache.get(path, 'data').get('range')
            if data_range == None or data_range[0].contains([offset, offset + length - 1]):
                break
            if not data_range[1].contains([offset, offset]):
                data_range[1].add([offset, offset + length - 1]) # To avoid starting the same thread again
                t = threading.Thread(target=self.download_data, args=(path, offset))
                t.daemon = True
                t.start()
            data_range[2].wait()
	# update atime just in the cache ???
        data = self.cache.get(path, 'data')
        with data.lock:
            data.content.seek(offset)
            return data.content.read(length)

    def write(self, path, new_data, offset, fh=None):
        logger.debug("write '%s' '%i' '%i' '%s'" % (path, len(new_data), offset, fh))
        if not self.cache.has(path) or self.cache.is_empty(path):
            logger.debug("write '%s' '%i' '%i' '%s' ENOENT" % (path, len(new_data), offset, fh))
            raise FuseOSError(errno.ENOENT)
	length = len(new_data)
        while True:
            data_range = self.cache.get(path, 'data').get('range')
            if data_range == None or data_range[0].contains([offset, offset + length - 1]):
                break
            data_range[2].wait()
        data = self.cache.get(path, 'data')
	with data.lock:
            data.content.seek(offset)
            data.content.write(new_data)
            data.set('change', True)
	    now = str(time.time())
	    attr = self.get_metadata(path, 'attr')
            old_size = int(attr['st_size'])
            new_size = max(old_size, offset + length)
            if new_size != old_size:
                attr['st_size'] = str(new_size)
                data.update_size(new_size - old_size)
            attr['st_mtime'] = now
            attr['st_atime'] = now
            self.set_metadata(path, 'attr', attr)
        return length

    def flush(self, path, fh=None):
        logger.debug("flush '%s' '%s'" % (path, fh))
        data = self.cache.get(path, 'data')
        if data and data.has('change'):
            k = self.get_key(path)
            if not k:
                k = Key(self.s3_bucket)
                k.key = self.join_prefix(path)
                self.cache.set(path, 'key', k)
            now = str(time.time())
            attr = self.get_metadata(path, 'attr', k)
            attr['st_atime'] = now
            attr['st_mtime'] = now
            self.set_metadata(path, 'attr', attr, k)
            xattr = self.get_metadata(path, 'xattr') # Do something better ???
            self.set_metadata(path, 'xattr', xattr, k)
            type = mimetypes.guess_type(path)[0] or 'application/octet-stream'
            data.content.seek(0) # Do I need this???
            if k.size == None:
                old_size = 0
            else:
                old_size = k.size
            written = False
            if self.multipart_num > 0:
                full_size = data.size
                if full_size > self.multipart_size:
                    k = self.multipart_upload(k.name, data, full_size,
                                              headers={'Content-Type': type}, metadata=k.metadata)
                    self.cache.set(path, 'key', k)
                    written = True
            if not written:
                k.set_contents_from_file(data.content, headers={'Content-Type': type})
            data.update_etag(k.etag[1:-1])
            data.delete('change')
            self.publish(['flush', path, k.etag[1:-1]])
        return 0

    def multipart_upload(self, key_path, data, full_size, headers, metadata):
        logger.debug("multipart_upload '%s' '%s' '%s'" % (key_path, data, headers))
        part_num = 0
        part_pos = 0
        part_queue = Queue.Queue()
        while part_pos < full_size:
            bytes_left = full_size - part_pos
            if bytes_left > self.multipart_size:
                part_size = self.multipart_size
            else:
                part_size = bytes_left
            part_num += 1
            part_queue.put([ part_num, PartOfFSData(data, part_pos, part_size) ])
            part_pos += part_size
            logger.debug("part from %i for %i" % (part_pos, part_size))
        logger.debug("initiate_multipart_upload '%s' '%s'" % (key_path, headers))
        mpu = self.s3_bucket.initiate_multipart_upload(key_path, headers=headers, metadata=metadata)
        num_threads = min(part_num, self.multipart_num)
        for i in range(num_threads):
            t = threading.Thread(target=self.part_upload, args=(mpu, part_queue))
            t.demon = True
            t.start()
            logger.debug("multipart_upload thread '%i' started" % i)
        logger.debug("multipart_upload all threads started '%s' '%s' '%s'" % (key_path, data, headers))
        part_queue.join()
        logger.debug("multipart_upload all threads joined '%s' '%s' '%s'" % (key_path, data, headers))
        if len(mpu.get_all_parts()) == part_num:
            new_key = mpu.complete_upload()
        else:
            mpu.cancel_upload()
            new_key = None
        return new_key

    def part_upload(self, mpu, part_queue):
        logger.debug("new thread!")
        try:
            while (True):
                logger.debug("trying to get a part from the queue")
                [ num, part ] = part_queue.get(False)
                retry = 0
                for retry in range(self.multipart_retries):
                    logger.debug("begin upload of part %i retry %i" % (num, retry))
                    try:
                        mpu.upload_part_from_file(fp=part, part_num=num) # Manage retries???
                    except:
                        logger.info("error during multipart upload part %i retry %i: %s"
                                    % (num, retry, sys.exc_info()[0]))
                        pass
                    else:
                        break
                logger.debug("end upload of part %i retry %i" % (num, retry))
                part_queue.task_done()
        except Queue.Empty:
            logger.debug("the queue is empty")
            
    def chmod(self, path, mode):
        logger.debug("chmod '%s' '%i'" % (path, mode))
        if self.cache.is_empty(path):
            logger.debug("chmod '%s' '%i' ENOENT" % (path, mode))
            raise FuseOSError(errno.ENOENT)
        attr = self.get_metadata(path, 'attr')
        if attr < 0:
            return attr
        attr['st_mode'] = str(mode)
        self.set_metadata(path, 'attr', attr)
        return 0

    def chown(self, path, uid, gid):
        logger.debug("chown '%s' '%i' '%i'" % (path, uid, gid))
        if self.cache.is_empty(path):
            logger.debug("chown '%s' '%i' '%i' ENOENT" % (path, uid, gid))
            raise FuseOSError(errno.ENOENT)
        attr = self.get_metadata(path, 'attr')
        if attr < 0:
            return attr
        attr['st_uid'] = str(uid)
        attr['st_gid'] = str(gid)
        self.set_metadata(path, 'attr', attr)
        return 0

    def utime(self, path, times=None):
        logger.debug("utime '%s' '%s'" % (path, times))
        if self.cache.is_empty(path):
            logger.debug("utime '%s' '%s' ENOENT" % (path, times))
            raise FuseOSError(errno.ENOENT)
        now = time.time()
        atime, mtime = times if times else (now, now)
        attr = self.get_metadata(path, 'attr')
        if attr < 0:
            return attr
        attr['st_atime'] = atime
        attr['st_mtime'] = mtime
        self.set_metadata(path, 'attr', attr)
        return 0

    def getxattr(self, path, name, position=0):
        logger.debug("getxattr '%s' '%s' '%i'" % (path, name, position))
        if self.cache.is_empty(path):
            logger.debug("getxattr '%s' '%s' '%i' ENOENT" % (path, name, position))
            raise FuseOSError(errno.ENOENT)
        xattr = self.get_metadata(path, 'xattr')
        try:
            return xattr[name]
        except KeyError:
            return '' # Should return ENOATTR

    def listxattr(self, path):
        logger.debug("listxattr '%s'" % (path))
        if self.cache.is_empty(path):
            logger.debug("listxattr '%s' ENOENT" % (path))
            raise FuseOSError(errno.ENOENT)
        xattr = self.get_metadata(path, 'xattr')
        return xattr.keys()

    def removexattr(self, path, name):
        logger.debug("removexattr '%s'" % (path, name))
        if self.cache.is_empty(path):
            logger.debug("removexattr '%s' ENOENT" % (path, name))
            raise FuseOSError(errno.ENOENT)
        xattr = self.get_metadata(path, 'xattr')
        try:
            del xattr[name]
            self.set_metadata(path, 'xattr', xattr)
        except KeyError:
            logger.debug("removexattr '%s' should ENOATTR" % (path, name))
            return '' # Should return ENOATTR
        return 0

    def setxattr(self, path, name, value, options, position=0):
        logger.debug("setxattr '%s' '%s' '%s' '%s' '%i'" % (path, name, value, options, position))
        if self.cache.is_empty(path):
            logger.debug("setxattr '%s' '%s' '%s' '%s' '%i' ENOENT" % (path, name, value, options, position))
            raise FuseOSError(errno.ENOENT)
        xattr = self.get_metadata(path, 'xattr')
        if xattr < 0:
            return xattr
        xattr[name] = value
        self.set_metadata(path, 'xattr', xattr)
        return 0

    def statfs(self, path):
        logger.debug("statfs '%s'" % (path))
        """Returns a dictionary with keys identical to the statvfs C
           structure of statvfs(3).
           The 'f_frsize', 'f_favail', 'f_fsid' and 'f_flag' fields are ignored
           On Mac OS X f_bsize and f_frsize must be a power of 2
           (minimum 512)."""
        return {
            "f_namemax" : 512,
            "f_bsize" : 1024 * 1024,
            "f_blocks" : 1024 * 1024 * 1024,
            "f_bfree" : 1024 * 1024 * 1024,
            "f_bavail" : 1024 * 1024 * 1024,
            "f_files" : 1024 * 1024 * 1024,
            "f_favail" : 1024 * 1024 * 1024,
            "f_ffree" : 1024 * 1024 * 1024
            }
        return {}

def errorAndExit(error, exitCode=1):
    logger.error(error + ", use -h for help.")
    exit(exitCode)

if __name__ == '__main__':

    usage = """%prog <mountpoint> [options]

YAS3FS (Yet Another S3-backed File System) is a Filesystem in Userspace (FUSE) interface to Amazon S3.

It allows to mount an S3 bucket (or a part of it, if you specify a path) as a local folder.
It works on Linux and Mac OS X.
For maximum speed all data read from S3 is cached locally on the node, in memory or on disk, depending of the file size.
Parallel multi-part downloads are used if there are reads in the middle of the file (e.g. for streaming).
Parallel multi-part uploads are used for files larger than a specified size.
With buffering enabled (the default) files can be accessed during the download from S3 (e.g. for streaming).
It can be used on more than one node to create a "shared" file system (i.e. a yas3fs "cluster").
SNS notifications are used to update other nodes in the cluster that something has changed on S3 and they need to invalidate their cache.
Notifications can be listened using HTTP or SQS endpoints.
If the cache grows to its maximum size, the less recently accessed files are removed.
AWS credentials can be passed using AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environmental variables.
In an EC2 instance a IAM role can be used to give access to S3/SNS/SQS resources."""

    parser = OptionParser(usage=usage)

    parser.add_option("--url", dest="url",
                      help="the S3 path to mount in s3://BUCKET/PATH format, "
                      + "PATH can be empty, can contain subfolders and is created on first mount if not found in the BUCKET",
                      metavar="URL")
    parser.add_option("--region", dest="region",
                      help="AWS region to use for SNS/SQS (default is %default)",
                      metavar="REGION", default="us-east-1")
    parser.add_option("--topic", dest="topic",
                      help="SNS topic ARN", metavar="ARN")
    parser.add_option("--hostname", dest="hostname",
                      help="hostname to listen to SNS HTTP notifications", metavar="HOST")
    parser.add_option("--ec2-hostname", action="store_true", dest="ec2_hostname", default=False,
                      help="get public hostname from EC2 instance metadata (overrides '--hostname')")
    parser.add_option("--port", dest="port",
                      help="TCP port to listen to SNS HTTP notifications", metavar="N")
    parser.add_option("--queue", dest="queue",
                      help="SQS queue name, a new queue is created if it doesn't exist", metavar="NAME")
    parser.add_option("--new-queue", action="store_true", dest="new_queue", default=False,
                      help="create a new SQS queue that is deleted on unmount (overrides '--queue', queue name is BUCKET-PATH-ID with alphanumeric characters only)")
    parser.add_option("--queue-wait", dest="queue_wait_time",
                      help="SQS queue wait time in seconds (using long polling, 0 to disable, default is %default seconds)", metavar="N", default=20)
    parser.add_option("--queue-polling", dest="queue_polling_interval",
                      help="SQS queue polling interval in seconds (default is %default seconds)", metavar="N", default=0)
    parser.add_option("--cache-entries", dest="cache_entries",
                      help="max number of entries to cache (default is %default entries)", metavar="N", default=1000000)
    parser.add_option("--cache-mem-size", dest="cache_mem_size",
                      help="max size of the memory cache in MB (default is %default MB)", metavar="N", default=1024)
    parser.add_option("--cache-disk-size", dest="cache_disk_size",
                      help="max size of the disk cache in MB (default is %default MB)", metavar="N", default=10240)
    parser.add_option("--cache-path", dest="cache_path",
                      help="local path to use for disk cache (default is '/tmp/yas3fs/BUCKET/PATH')", metavar="PATH", default="")
    parser.add_option("--cache-on-disk", dest="cache_on_disk",
                      help="use disk (instead of memory) cache for files greater than the given size in MB (default is %default MB)",
                      metavar="N", default=100)
    parser.add_option("--cache-check", dest="cache_check_interval",
                      help="interval between cache memory checks in seconds (default is %default seconds)", metavar="N", default=10)
    parser.add_option("--buffer-size", dest="buffer_size",
                      help="download buffer size in KB (0 to disable buffering, default is %default KB)", metavar="N", default=10240)
    parser.add_option("--no-metadata", action="store_false", dest="write_metadata", default=True,
                      help="don't write user metadata on S3 to persist file system attr/xattr")
    parser.add_option("--prefetch", action="store_true", dest="prefetch", default=False,
                      help="start downloading file content as soon as the file is discovered")
    parser.add_option("--mp-size", dest="multipart_size",
                      help="size of parts to use for multipart upload in KB (default value is %default KB, the minimum allowed is 5120 KB)", metavar="N", default=10240)
    parser.add_option("--mp-num", dest="multipart_num",
                      help="max number of parallel multipart uploads per file (0 to disable multipart upload, default is %default)", metavar="N", default=4)
    parser.add_option("--mp-retries", dest="multipart_retries",
                      help="max number of retries in uploading a part (default is %default)", metavar="N", default=3)
    parser.add_option("--id", dest="id",
                      help="a unique ID identifying this node in a cluster", metavar="ID")
    parser.add_option("--log", dest="logfile",
                      help="the filename to use for logs", metavar="FILE", default="")
    parser.add_option("--mkdir", action="store_true", dest="mkdir", default=False,
                      help="create mountpoint if not found (create intermediate directories as required)")
    parser.add_option("-f", "--foreground", action="store_true", dest="foreground", default=False,
                      help="run in foreground")
    parser.add_option("-d", "--debug", action="store_true", dest="debug", default=False,
                      help="print debug information (implies '-f')")

    (options, args) = parser.parse_args()

    logging.basicConfig()
    logger = logging.getLogger('yas3fs')

    if options.logfile != '':
        logHandler = logging.handlers.RotatingFileHandler(options.logfile, maxBytes=1024*1024, backupCount=10)
        logger.addHandler(logHandler)

    if options.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
        
    if len(args) < 1:
        errorAndExit("mountpoint must be provided")
    elif len(args) > 1:
        errorAndExit("not more than one mountpoint must be provided")

    mountpoint = args[0]

    if options.mkdir:
        try:
            os.makedirs(mountpoint)
        except OSError as exc: # Python >2.5
            if exc.errno == errno.EEXIST and os.path.isdir(mountpoint):
                pass
            else:
                raise

    if sys.platform == "darwin":
        volume_name = os.path.basename(mountpoint)
        fuse = FUSE(YAS3FS(options), mountpoint, fsname="yas3fs",
                    foreground=options.foreground or options.debug,
                    default_permissions=True, allow_other=True,
                    auto_cache=True,
                    auto_xattr=True, volname=volume_name,
                    noappledouble=True, daemon_timeout=3600,
                    local=True)
    else:
        fuse = FUSE(YAS3FS(options), mountpoint, fsname="yas3fs",
                    foreground=options.foreground or options.debug,
                    default_permissions=True, allow_other=True,
                    auto_cache=True)
