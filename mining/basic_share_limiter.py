import lib.settings as settings

import lib.logger
log = lib.logger.get_logger('BasicShareLimiter')

import DBInterface
dbi = DBInterface.DBInterface()

# Only clear worker difficulties in the database when external difficulty is disabled
if not settings.ALLOW_EXTERNAL_DIFFICULTY:
	dbi.clear_worker_diff()

from twisted.internet import defer
from mining.interfaces import Interfaces
import time

''' This is just a customized ring buffer '''
class SpeedBuffer:
	def __init__(self, size_max):
		self.max = size_max
		self.data = []
		self.cur = 0
		
	def append(self, x):
		self.data.append(x)
		self.cur += 1
		if len(self.data) == self.max:
			self.cur = 0
			self.__class__ = SpeedBufferFull
			
	def avg(self):
		return sum(self.data) / self.cur
	   
	def pos(self):
		return self.cur
		   
	def clear(self):
		self.data = []
		self.cur = 0
			
	def size(self):
		return self.cur

class SpeedBufferFull:
	def __init__(self, n):
		raise "you should use SpeedBuffer"
		   
	def append(self, x):				
		self.data[self.cur] = x
		self.cur = (self.cur + 1) % self.max
			
	def avg(self):
		return sum(self.data) / self.max
		   
	def pos(self):
		return self.cur
		   
	def clear(self):
		self.data = []
		self.cur = 0
		self.__class__ = SpeedBuffer
			
	def size(self):
		return self.max

class BasicShareLimiter(object):
	def __init__(self):
		self.worker_stats = {}
		self.target = settings.VDIFF_TARGET_TIME
		self.retarget = settings.VDIFF_RETARGET_TIME
		self.variance = self.target * (float(settings.VDIFF_VARIANCE_PERCENT) / float(100))
		self.tmin = self.target - self.variance
		self.tmax = self.target + self.variance
		self.buffersize = self.retarget / self.target * 4
		self.coind = {}
		self.coind_diff = 100000000 # TODO: Set this to VARDIFF_MAX
		# TODO: trim the hash of inactive workers

	@defer.inlineCallbacks
	def update_litecoin_difficulty(self):
		# Cache the coind difficulty so we do not have to query it on every submit
		# Update the difficulty  if it is out of date or not set
		if 'timestamp' not in self.coind or self.coind['timestamp'] < int(time.time()) - settings.DIFF_UPDATE_FREQUENCY:
			self.coind['timestamp'] = time.time()
			self.coind['difficulty'] = (yield Interfaces.template_registry.bitcoin_rpc.getdifficulty())
			log.debug("Updated coin difficulty to %s" %  (self.coind['difficulty']))
		self.coind_diff = self.coind['difficulty']

	def set_worker_difficulty(self, connection_ref, new_diff, worker_name, force_new = False):
		# Sets the subscribed work a new difficulty and assignes new work
		# Old jobs may be forced cleared

		log.info("Setting difficulty for %s To: %i" % (worker_name, new_diff))

		# May not always be set
		if worker_name in self.worker_stats:
			self.worker_stats[worker_name]['buffer'].clear()

		session = connection_ref().get_session()

		(job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, _) = \
			Interfaces.template_registry.get_last_broadcast_args()
		work_id = Interfaces.worker_manager.register_work(worker_name, job_id, new_diff)

		session['prev_diff'] = session['difficulty']
		session['prev_jobid'] = job_id
		session['difficulty'] = new_diff
		connection_ref().rpc('mining.set_difficulty', [new_diff, ], is_notification=True)
		log.debug("Notified of New Difficulty")
		connection_ref().rpc('mining.notify', [work_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, force_new, ], is_notification=True)
		log.debug("Sent new work")
		dbi.update_worker_diff(worker_name, new_diff)

	def submit(self, connection_ref, job_id, current_difficulty, timestamp, worker_name):
		timestamp = int(timestamp)

		# Init the stats for this worker if it isn't set.
		if worker_name not in self.worker_stats or self.worker_stats[worker_name]['last_ts'] < timestamp - settings.DB_USERCACHE_TIME :
			# Load the worker's difficult as set in the database
			(use_vardiff, database_worker_difficulty) = Interfaces.worker_manager.get_user_difficulty(worker_name)
			log.info("Database difficulty for %s Found as: %s.  Curent diff is: %s Using VARDIFF: %s" % (worker_name, database_worker_difficulty, current_difficulty, ('Yes' if use_vardiff else 'No')))
			# Set it to current difficulty
			dbi.update_worker_diff(worker_name, current_difficulty)
			# Cache the information
			self.worker_stats[worker_name] = {'last_rtc': (timestamp - self.retarget / 2), 'last_ts': timestamp, 'buffer': SpeedBuffer(self.buffersize), 'database_worker_difficulty': database_worker_difficulty, 'use_vardiff': use_vardiff}
			return

		# Standard share update of data
		self.worker_stats[worker_name]['buffer'].append(timestamp - self.worker_stats[worker_name]['last_ts'])
		self.worker_stats[worker_name]['last_ts'] = timestamp

		# If not using VARDIFF (from cached value) we are done here
		if not self.worker_stats[worker_name]['use_vardiff']:
			return

		# Do We retarget? If not, we're done.
		if timestamp - self.worker_stats[worker_name]['last_rtc'] < self.retarget and self.worker_stats[worker_name]['buffer'].size() > 0:
			return

		# Set up and log our check
		self.worker_stats[worker_name]['last_rtc'] = timestamp
		avg = self.worker_stats[worker_name]['buffer'].avg()
		log.info("Checking Retarget for %s (%s) avg. %s target %s+-%s" % (worker_name, current_difficulty, avg,
				self.target, self.variance))
		
		if avg < 1:
			log.info("Reseting avg = 1 since it's SOOO low")
			avg = 1

		# Figure out our Delta-Diff
		if settings.VDIFF_FLOAT:
			ddiff = float((float(current_difficulty) * (float(self.target) / float(avg))) - current_difficulty)
		else:
			ddiff = int((float(current_difficulty) * (float(self.target) / float(avg))) - current_difficulty)

		if (avg > self.tmax):
			# For fractional -0.1 ddiff's just drop by 1
			if settings.VDIFF_X2_TYPE:
				ddiff = 0.5
				# Don't drop below MIN_TARGET
				if (ddiff * current_difficulty) < settings.VDIFF_MIN_TARGET:
					ddiff = settings.VDIFF_MIN_TARGET / current_difficulty
			else:
				if ddiff > -settings.VDIFF_MIN_CHANGE:
					ddiff = -settings.VDIFF_MIN_CHANGE
				# Don't drop below POOL_TARGET
				if (ddiff + current_difficulty) < settings.VDIFF_MIN_TARGET:
					ddiff = settings.VDIFF_MIN_TARGET - current_difficulty
		elif avg < self.tmin:
			# For fractional 0.1 ddiff's just up by 1
			if settings.VDIFF_X2_TYPE:
				ddiff = 2
				# Don't go above COINDAEMON or VDIFF_MAX_TARGET
				if settings.USE_COINDAEMON_DIFF:
					self.update_litecoin_difficulty()
					diff_max = min([settings.VDIFF_MAX_TARGET, self.coind_diff])
				else:
					diff_max = settings.VDIFF_MAX_TARGET

				if (ddiff * current_difficulty) > diff_max:
					ddiff = diff_max / current_difficulty
			else:
				if ddiff < settings.VDIFF_MIN_CHANGE:
					ddiff = settings.VDIFF_MIN_CHANGE
				# Don't go above COINDAEMON or VDIFF_MAX_TARGET
				if settings.USE_COINDAEMON_DIFF:
					self.update_litecoin_difficulty()
					diff_max = min([settings.VDIFF_MAX_TARGET, self.coind_diff])
				else:
					diff_max = settings.VDIFF_MAX_TARGET

				if (ddiff + current_difficulty) > diff_max:
					ddiff = diff_max - current_difficulty
			
		else:  # If we are here, then we should not be retargeting.
			return

		# At this point we are retargeting this worker
		if settings.VDIFF_X2_TYPE:
			new_diff = current_difficulty * ddiff
		else:
			new_diff = current_difficulty + ddiff

		# Do the difficulty retarget
		log.info("Retarget for %s %i old: %i new: %i" % (worker_name, ddiff, current_difficulty, new_diff))
		self.set_worker_difficulty(connection_ref, new_diff, worker_name)

