import logging
from base64 import b64decode, b64encode

try:
    import larch.pickle as serializer
except ImportError:
    try:
        import cPickle as serializer
    except ImportError:
        import pickle as serializer
    logging.warning("Using cPickle/pickler as serializer, it's INSECURE ! Install larch.pickle to fix.")

def loads(string):
    return serializer.loads(string)

def dumps(obj, protocol=2):
    print "kikoo"
    l = serializer.dumps(obj, protocol)
    print l
    return l

def load(fname):
    return serializer.load(fname)

class picklev2:
    @staticmethod 
    def loads(string):
        return serializer.loads(string)

    @staticmethod 
    def dumps(obj):
        return serializer.dumps(obj, protocol=2)

    @staticmethod
    def load(fname):
        return serializer.load(fname)

    @staticmethod
    def dump(fname):
        return serializer.dump(fname)
    
class b64serializer:
    @staticmethod 
    def loads(string):
        return serializer.loads(b64decode(string))

    @staticmethod 
    def dumps(obj, protocol=2):
        return b64encode(serializer.dumps(obj, protocol))
