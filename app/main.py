import logging
import os
import re
import shutil
import sys
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR)

from flask import Flask, send_file, request, abort, g, jsonify
from flask.json import JSONEncoder
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from werkzeug.utils import secure_filename
from webwhatsapi import MessageGroup, WhatsAPIDriver, WhatsAPIDriverStatus
from webwhatsapi.objects.whatsapp_object import WhatsappObject

"""
###########################
##### CLASS DEFINITION ####
###########################
"""

class RepeatedTimer(object):
    """
    A generic class that creates a timer of specified interval and calls the
    given function after that interval
    """

    def __init__(self, interval, function, *args, **kwargs):
        """ Starts a timer of given interval
        @param self:
        @param interval: Wait time between calls
        @param function: Function object that is needed to be called
        @param *args: args to pass to the called functions
        @param *kwargs: args to pass to the called functions
        """
        self._timer = None
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.is_running = False
        self.start()

    def _run(self):
        self.is_running = False
        self.start()
        self.function(*self.args, **self.kwargs)

    def start(self):
        """Creates a timer and start it"""

        if not self.is_running:
            self._timer = threading.Timer(self.interval, self._run)
            self._timer.start()
            self.is_running = True

    def stop(self):
        """Stop the timer"""
        self._timer.cancel()
        self.is_running = False

class WhatsAPIJSONEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, WhatsappObject):
            return obj.get_js_obj()
        if isinstance(obj, MessageGroup):
            return obj.chat
        return super(WhatsAPIJSONEncoder, self).default(obj)

"""
###########################
##### GLOBAL VARIABLES ####
###########################
"""

app = Flask(__name__)
app.json_encoder = WhatsAPIJSONEncoder

# Logger
logger = None
log_file = "app/log/log.txt"
log_level = logging.INFO
# Driver store all the instances of webdriver for each of the client user
drivers = dict()
# Store all timer objects for each client user
timers = dict()
# Store list of semaphores
semaphores = dict()

# API key needed for auth with this API, change as per usage
API_KEY = os.environ.get("API_KEY")

# File type allowed to be sent or received
ALLOWED_EXTENSIONS = (
    "avi",
    "mp4",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "mp3",
    "doc",
    "docx",
    "pdf",
)

# Path to temporarily store static files like images
STATIC_FILES_PATH = BASE_DIR + "/app/app/static/"

# Seleneium Webdriver configuration
FIREFOX_CACHE_PATH = BASE_DIR + "/app/app/firefox_cache/"

"""
##############################
##### FUNCTION DEFINITION ####
##############################
"""

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.driver_status != WhatsAPIDriverStatus.LoggedIn:
            return jsonify({"error": "client is not logged in"})
        return f(*args, **kwargs)

    return decorated_function

def create_logger():
    """Initial the global logger variable"""
    global logger

    formatter = logging.Formatter("%(asctime)s|%(levelname)s|%(message)s")
    handler = TimedRotatingFileHandler(log_file, when="midnight", interval=1)
    handler.setFormatter(formatter)
    handler.setLevel(log_level)
    handler.suffix = "%Y-%m-%d"
    logger = logging.getLogger("sacplus")
    logger.setLevel(log_level)
    logger.addHandler(handler)

def init_driver(client_id):
    """Initialises a new driver via webwhatsapi module
    
    @param client_id: ID of user client
    @return webwhatsapi object
    """

    # Create profile directory if it does not exist
    profile_path = FIREFOX_CACHE_PATH + str(client_id)
    if not os.path.exists(profile_path):
        os.makedirs(profile_path)

    # Create a whatsapidriver object
    d = WhatsAPIDriver(
        username=client_id,
        profile=profile_path,
        client="remote",
        command_executor=os.environ["SELENIUM"],
    )
    return d


def init_client(client_id):
    """Initialse a driver for client and store for future reference
    
    @param client_id: ID of client user
    @return whebwhatsapi object
    """
    if client_id not in drivers:
        drivers[client_id] = init_driver(client_id)
    return drivers[client_id]


def delete_client(client_id, preserve_cache):
    """Delete all objects related to client
    
    @param client_id: ID of client user
    @param preserve_cache: Boolean, whether to delete the chrome profile folder or not
    """
    if client_id in drivers:
        drivers.pop(client_id).quit()
        try:
            timers[client_id].stop()
            timers[client_id] = None
            release_semaphore(client_id)
            semaphores[client_id] = None
        except:
            pass

    if not preserve_cache:
        pth = FIREFOX_CACHE_PATH + g.client_id
        shutil.rmtree(pth)

def init_timer(client_id):
    """Create a timer for the client driver to watch for events
    
    @param client_id: ID of clinet user
    """
    if client_id in timers and timers[client_id]:
        timers[client_id].start()
        return
    # Create a timer to call check_new_message function after every 2 seconds.
    # client_id param is needed to be passed to check_new_message
    timers[client_id] = RepeatedTimer(2, check_new_messages, client_id)

def check_new_messages(client_id):
    """Check for new unread messages and send them to the custom api

    @param client_id: ID of client user
    """
    # Return if driver is not defined or if whatsapp is not logged in.
    # Stop the timer as well
    if (
        client_id not in drivers
        or not drivers[client_id]
        or not drivers[client_id].is_logged_in()
    ):
        timers[client_id].stop()
        return

    # Acquire a lock on thread
    if not acquire_semaphore(client_id, True):
        return

    try:
        # Get all unread messages
        res = drivers[client_id].get_unread()
        # Mark all of them as seen
        for message_group in res:
            message_group.chat.send_seen()
        # Release thread lock
        release_semaphore(client_id)
        # If we have new messages, do something with it
        if res:
            print(res)
    except:
        pass
    finally:
        # Release lock anyway, safekeeping
        release_semaphore(client_id)

def get_client_info(client_id):
    """Get the status of a perticular client, as to he/she is connected or not
    
    @param client_id: ID of client user
    @return JSON object {
        "driver_status": webdriver status
        "is_alive": if driver is active or not
        "is_logged_in": if user is logged in or not
        "is_timer": if timer is running or not
    }
    """
    if client_id not in drivers:
        return None

    driver_status = drivers[client_id].get_status()
    is_alive = False
    is_logged_in = False
    if (
        driver_status == WhatsAPIDriverStatus.NotLoggedIn
        or driver_status == WhatsAPIDriverStatus.LoggedIn
    ):
        is_alive = True
    if driver_status == WhatsAPIDriverStatus.LoggedIn:
        is_logged_in = True

    return {
        "is_alive": is_alive,
        "is_logged_in": is_logged_in,
        "is_timer": bool(timers[client_id]) and timers[client_id].is_running,
    }

def allowed_file(filename):
    """Check if file as allowed type or not
    
    @param filename: Name of the file to be checked
    @return boolean True or False based on file name check
    """
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def send_media(chat_id, requestObj):
    files = requestObj.files
    if not files:
        return jsonify({"Status": False})

    # create user folder if not exists
    profile_path = create_static_profile_path(g.client_id)

    file_paths = []
    for file in files:
        file = files.get(file)
        if file.filename == "":
            return {"Status": False}

        if not file or not allowed_file(file.filename):
            return {"Status": False}

        filename = secure_filename(file.filename)

        # save file
        file_path = os.path.join(profile_path, filename)
        file.save(file_path)
        file_path = os.path.join(os.getcwd(), file_path)

        file_paths.append(file_path)

    caption = requestObj.form.get("message")

    res = None
    for file_path in file_paths:
        res = g.driver.send_media(file_path, chat_id, caption)
    return res

def create_static_profile_path(client_id):
    """Create a profile path folder if not exist
    
    @param client_id: ID of client user
    @return string profile path
    """
    profile_path = os.path.join(STATIC_FILES_PATH, str(client_id))
    if not os.path.exists(profile_path):
        os.makedirs(profile_path)
    return profile_path

def acquire_semaphore(client_id, cancel_if_locked=False):
    if not client_id:
        return False

    if client_id not in semaphores:
        semaphores[client_id] = threading.Semaphore()

    timeout = 10
    if cancel_if_locked:
        timeout = 0

    val = semaphores[client_id].acquire(blocking=True, timeout=timeout)

    return val

def release_semaphore(client_id):
    if not client_id:
        return False

    if client_id in semaphores:
        semaphores[client_id].release()

@app.before_request
def before_request():
    """This runs before every API request. The function take cares of creating
    driver object is not already created. Also it checks for few prerequisits
    parameters and set global variables for other functions to use
    
    Required paramters for an API hit are:
    auth-key: key string to identify valid request
    client_id: to identify for which client the request is to be run
    """
    global logger

    if not request.url_rule:
        abort(404)

    if logger == None:
        create_logger()
    logger.info("API call " + request.method + " " + request.url)

    auth_key = request.headers.get("auth-key")
    g.client_id = request.headers.get("client-id")
    rule_parent = request.url_rule.rule.split("/")[1]

    if API_KEY and auth_key != API_KEY:
        abort(401, "you must send valid auth-key")
        raise Exception()

    if not g.client_id and rule_parent != "admin":
        abort(400, "Client ID is mandatory")

    acquire_semaphore(g.client_id)

    # Create a driver object if not exist for client requests.
    if rule_parent != "admin":
        if g.client_id not in drivers:
            drivers[g.client_id] = init_client(g.client_id)

        g.driver = drivers[g.client_id]
        g.driver_status = WhatsAPIDriverStatus.Unknown

        if g.driver is not None:
            g.driver_status = g.driver.get_status()

        # If driver status is unkown, means driver has closed somehow, reopen it
        if (
            g.driver_status != WhatsAPIDriverStatus.NotLoggedIn
            and g.driver_status != WhatsAPIDriverStatus.LoggedIn
        ):
            drivers[g.client_id] = init_client(g.client_id)
            g.driver_status = g.driver.get_status()

        init_timer(g.client_id)

@app.after_request
def after_request(r):
    """This runs after every request end. Purpose is to release the lock acquired
    during staring of API request"""
    if "client_id" in g and g.client_id:
        release_semaphore(g.client_id)
    return r

"""
#####################
##### API ROUTES ####
#####################
"""

@app.route("/")
def hello():
    return "Welcome to Whatsapp API"

@app.route("/screen", methods=["GET"])
def get_screen():
    """Capture chrome screen image and send it back."""
    img_title = "screen_" + g.client_id + ".png"
    image_path = STATIC_FILES_PATH + img_title
    g.driver.screenshot(image_path)
    return send_file(image_path, mimetype="image/png")

@app.route("/client", methods=["PUT"])
def create_client():
    """Create a new client driver. The driver is automatically created in 
    before_request function."""
    result = False
    if g.client_id in drivers:
        result = True
    return jsonify({"Success": result})

@app.route("/client", methods=["DELETE"])
def delete_client():
    """Delete all objects related to client"""
    preserve_cache = request.args.get("preserve_cache", False)
    delete_client(g.client_id, preserve_cache)
    return jsonify({"Success": True})

@app.route("/auth", methods=["GET"])
def get_qr():
    """Get qr as image"""
    img_title = "screen_" + g.client_id + ".png"
    image_path = STATIC_FILES_PATH + img_title
    g.driver.get_qr(image_path)
    return send_file(image_path, mimetype="image/png")

@app.route("/auth/plain", methods=["GET"])
def get_qr_plain():
    """Get qr as a json string"""
    qr = g.driver.get_qr_plain()
    return jsonify({"qr": qr})

@app.route("/auth/here", methods=["GET"])
def get_open_here():
    """Open whatsapp in here"""
    g.driver.open_here()
    return jsonify({"success": True})

@app.route("/chats", methods=["POST"])
@login_required
def new_chat():
    """Return the new chat"""
    number = request.form.get("number")
    if not number:
        abort(400, "Phone Number is mandatory")

    number = re.sub(r'[^0-9]', '', number)
    if number[0] == '0':
        abort(400, "Use Country Code")

    result = g.driver.get_chat_from_phone_number(number, True)
    return jsonify(result)

@app.route("/chats", methods=["GET"])
@login_required
def get_chats():
    """Return all the chats"""
    result = g.driver.get_all_chats()
    return jsonify(result)

@app.route("/chats/<chat_id>/messages", methods=["GET"])
@login_required
def get_messages(chat_id):
    """Return all of the chat messages"""
    mark_seen = request.args.get("mark_seen", True)
    include_me = request.args.get("include_me", False)
    include_notifications = request.args.get("include_notifications", False)

    chat = g.driver.get_chat_from_id(chat_id)
    msgs = list(g.driver.get_all_messages_in_chat(chat, include_me, include_notifications))
    for msg in msgs:
        print(msg.id)
    if mark_seen:
        for msg in msgs:
            try:
                msg.chat.send_seen()
            except:
                pass
    return jsonify(msgs)

@app.route("/chats/<chat_id>/messages", methods=["POST"])
@login_required
def send_message(chat_id):
    """Send a message to a chat
    If a media file is found, send_media is called, else a simple text message
    is sent
    """
    files = request.files
    if files:
        res = send_media(chat_id, request)
    else:
        message = request.form.get("message")
        res = g.driver.chat_send_message(chat_id, message)

    if res:
        return jsonify(res)
    else:
        return False

# --------------------------- Admin methods ----------------------------------

@app.route("/admin/clients", methods=["GET"])
def get_active_clients():
    """Get a list of all active clients and their status"""
    global drivers

    if not drivers:
        return jsonify([])

    result = {client: get_client_info(client) for client in drivers}
    return jsonify(result)

@app.route("/admin/clients", methods=["PUT"])
def run_clients():
    """Force create driver for client """
    clients = request.form.get("clients")
    if not clients:
        return jsonify({"Error": "no clients provided"})

    result = {}
    for client_id in clients.split(","):
        if client_id not in drivers:
            init_client(client_id)
            init_timer(client_id)

        result[client_id] = get_client_info(client_id)

    return jsonify(result)

@app.route("/admin/clients", methods=["DELETE"])
def kill_clients():
    """Force kill driver and other objects for a perticular clien"""
    clients = request.form.get("clients").split(",")
    kill_dead = request.args.get("kill_dead", default=False)
    kill_dead = kill_dead and kill_dead in ["true", "1"]

    if not kill_dead and not clients:
        return jsonify({"Error": "no clients provided"})

    for client in list(drivers.keys()):
        if kill_dead and not drivers[client].is_logged_in() or client in clients:
            drivers.pop(client).quit()
            try:
                timers[client].stop()
                timers[client] = None
                release_semaphore(client)
                semaphores[client] = None
            except:
                pass

    return get_active_clients()

@app.route("/admin/exception", methods=["GET"])
def get_last_exception():
    """Get last exception"""
    return jsonify(sys.exc_info())

if __name__ == "__main__":
    # Only for debugging while developing
    app.run(host="0.0.0.0", debug=True)
