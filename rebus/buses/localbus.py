import logging
import threading
from collections import Counter, defaultdict, namedtuple
from rebus.bus import Bus, DEFAULT_DOMAIN
from rebus.storage_backends.ramstorage import RAMStorage
from rebus.storage import StorageRegistry
from rebus.tools.config import get_output_altering_options
from rebus.tools.sched import Sched
from rebus.tools import format_check

log = logging.getLogger("rebus.localbus")
agent_desc = namedtuple("agent_desc", ("agent_id", "domain"))


@Bus.register
class LocalBus(Bus):
    _name_ = "localbus"

    def __init__(self, options):
        Bus.__init__(self)
        #: stores currently held locks [(lockid, domain, selector)]
        self.locks = defaultdict(set)
        #: Next available agent id. Never decreases.
        self.agent_count = 0
        self.store = RAMStorage()  # TODO add support for DiskStorage ?
        # TODO save internal state at bus exit (only useful with DiskStorage)
        #: maps agentid (ex. inject-12) to agentdesc
        self.agent_descs = {}
        #: maps agentid to agent instance
        self.agents = {}
        self.threads = []
        #: maps agentids to their serialized configuration - output altering
        #: options only
        self.agents_output_altering_options = {}
        #: maps agentids to their serialized configuration
        self.agents_full_config_txts = {}
        #: monotonically increasing user request counter
        self.userrequestid = 0
        #: retry_counters[(agent_name, config_txt, domain, selector)] = \
        #:     number of remaining retries
        self.retry_counters = defaultdict(dict)
        self.sched = Sched(self._sched_inject)

    def join(self, agent, agent_domain=DEFAULT_DOMAIN):
        agid = "%s-%i" % (agent.name, self.agent_count)
        self.agent_count += 1
        self.agents_full_config_txts[agid] = agent.config_txt
        self.agents_output_altering_options[agid] = \
            get_output_altering_options(agent.config_txt)
        self.agent_descs[agid] = agent_desc(agid, agent_domain)
        self.agents[agid] = agent
        return agid

    def lock(self, agent_id, lockid, desc_domain, selector):
        key = (lockid, desc_domain, selector)
        log.info("LOCK:%s %s => %r %s:%s", lockid, agent_id, key in
                 self.locks[desc_domain], desc_domain, selector)
        if key in self.locks[desc_domain]:
            return False
        self.locks[desc_domain].add(key)
        return True

    def unlock(self, agent_id, lockid, desc_domain, selector,
               processing_failed, retries, wait_time):
        lkey = (lockid, desc_domain, selector)
        log.info("UNLOCK:%s %s => %r %s:%s", lockid, agent_id, lkey in
                 self.locks[desc_domain], desc_domain, selector)
        if lkey not in self.locks[desc_domain]:
            return
        self.locks[desc_domain].remove(lkey)
        agent_name = self.agents[agent_id].name
        config_txt = self.agents_output_altering_options[agent_id]
        rkey = (agent_name, config_txt, desc_domain, selector)
        if rkey not in self.retry_counters:
            self.retry_counters[rkey] = retries
        if self.retry_counters[rkey] > 0:
            self.retry_counters[rkey] -= 1
            desc = self.store.get_descriptor(desc_domain, selector)
            uuid = desc.uuid
            self.sched.add_action(wait_time, (agent_id, desc_domain, uuid,
                                              selector, agent_name))

    def push(self, agent_id, descriptor):
        desc_domain = descriptor.domain
        selector = descriptor.selector
        # ensure processing terminates
        if not format_check.processing_depth(self.store, descriptor):
            log.warning("Refusing descriptor %s:%s: loop or >2 ancestors "
                        "having the same descriptor", desc_domain, selector)
            return False

        if self.store.add(descriptor):
            log.info("PUSH: %s => %s:%s", agent_id, desc_domain, selector)
            for agid in self.agents:
                try:
                    log.debug("Calling %s's on_new_descriptor", agid)
                    self.agents[agid].on_new_descriptor(agent_id,
                                                        desc_domain,
                                                        descriptor.uuid,
                                                        selector, 0)
                except Exception as e:
                    log.error("ERROR agent [%s]: %s", agid, e, exc_info=1)
        else:
            log.info("PUSH: %s already seen => %s:%s", agent_id, desc_domain,
                     selector)

    def get(self, agent_id, desc_domain, selector):
        log.info("GET: %s %s:%s", agent_id, desc_domain, selector)
        return self.store.get_descriptor(desc_domain, selector)

    def get_value(self, agent_id, desc_domain, selector):
        log.info("GET: %s %s:%s", agent_id, desc_domain, selector)
        return self.store.get_value(desc_domain, selector)

    def list_uuids(self, agent_id, desc_domain):
        log.debug("LISTUUIDS: %s %s", agent_id, desc_domain)
        return self.store.list_uuids(desc_domain)

    def find(self, agent_id, desc_domain, selector_regex, limit=0, offset=0):
        log.debug("FIND: %s %s:%s (max %d skip %d)", agent_id, desc_domain,
                  selector_regex, limit, offset)
        return self.store.find(desc_domain, selector_regex, limit, offset)

    def find_by_selector(self, agent_id, desc_domain, selector_prefix, limit=0,
                         offset=0):
        log.debug("FINDBYVALUE: %s %s %s (max %d skip %d)", agent_id,
                  desc_domain, selector_prefix, limit, offset)
        return self.store.find_by_selector(desc_domain, selector_prefix, limit,
                                           offset)

    def find_by_uuid(self, agent_id, desc_domain, uuid):
        log.debug("FINDBYUUID: %s %s:%s", agent_id, desc_domain, uuid)
        return self.store.find_by_uuid(desc_domain, uuid)

    def find_by_value(self, agent_id, desc_domain, selector_prefix,
                      value_regex):
        log.debug("FINDBYVALUE: %s %s %s %s", agent_id, desc_domain,
                  selector_prefix, value_regex)
        return self.store.find_by_value(desc_domain, selector_prefix,
                                        value_regex)

    def mark_processed(self, agent_id, desc_domain, selector):
        agent_name = self.agents[agent_id].name
        config_txt = self.agents_output_altering_options[agent_id]
        log.debug("MARK_PROCESSED: %s:%s %s %s", desc_domain, selector,
                  agent_id, config_txt)
        self.store.mark_processed(desc_domain, selector, agent_name,
                                  config_txt)

    def mark_processable(self, agent_id, desc_domain, selector):
        agent_name = self.agents[agent_id].name
        config_txt = self.agents_output_altering_options[agent_id]
        log.debug("MARK_PROCESSABLE: %s:%s %s %s", desc_domain, selector,
                  agent_id, config_txt)
        self.store.mark_processable(desc_domain, selector, agent_name,
                                    config_txt)

    def get_processable(self, agent_id, desc_domain, selector):
        log.debug("GET_PROCESSABLE: %s:%s %s", desc_domain, selector, agent_id)
        return self.store.get_processable(desc_domain, selector)

    def list_agents(self, agent_id):
        log.debug("LIST_AGENTS: %s", agent_id)
        return dict(Counter(i.rsplit('-', 1)[0] for i in self.agent_descs))

    def processed_stats(self, agent_id, desc_domain):
        log.debug("PROCESSED_STATS: %s %s", agent_id, desc_domain)
        return self.store.processed_stats(desc_domain)

    def get_children(self, agent_id, desc_domain, selector, recurse=True):
        log.info("GET_CHILDREN: %s %s:%s", agent_id, desc_domain, selector)
        return list(self.store.get_children(desc_domain, selector,
                                            recurse))

    def store_internal_state(self, agent_id, state):
        log.debug("STORE_INTSTATE: %s", agent_id)
        if self.store.STORES_INTSTATE:
            agent_name = self.agents[agent_id].name
            self.store.store_agent_state(agent_name, str(state))

    def load_internal_state(self, agent_id):
        log.debug("LOAD_INTSTATE: %s", agent_id)
        if self.store.STORES_INTSTATE:
            agent_name = self.agents[agent_id].name
            return self.store.load_agent_state(agent_name)
        return ""

    def request_processing(self, agent_id, desc_domain, selector,
                           targets):
        log.debug("REQUEST_PROCESSING: %s %s:%s target %s", agent_id,
                  desc_domain, selector, targets)
        self.userrequestid += 1
        d = self.store.get_descriptor(desc_domain, selector)
        for agid in self.agents:
            if self.agents[agid].name in targets:
                try:
                    log.debug("Calling %s on_new_descriptor for user-requested"
                              " processing", agid)
                    self.agents[agid].on_new_descriptor(agent_id,
                                                        desc_domain,
                                                        d.uuid,
                                                        selector,
                                                        self.userrequestid)
                except Exception as e:
                    log.error("ERROR agent [%s]: %s", agid, e, exc_info=1)

    def busthread_call(self, method, *params):
        # Caution - there are several bus threads with this mode - typically 1
        # per inject thread.
        method(*params)

    def _sched_inject(self, agent_id, desc_domain, uuid, selector, target):
        """
        Called by Sched object, from Timer thread. Emits targeted_descriptor
        through bus thread.
        """
        self.busthread_call(
            self.agents[agent_id].on_new_descriptor,
            *(agent_id, desc_domain, uuid, selector, 0))

    def run_agents(self):
        for agent in self.agents.values():
            t = threading.Thread(target=agent.run_and_catch_exc)
            t.daemon = True
            t.start()
            self.threads.append(t)
        for t in self.threads:
            t.join()
        new_descs = True
        while new_descs:
            new_descs = False
            for agent in self.agents.values():
                new_descs = new_descs or agent.on_idle()
