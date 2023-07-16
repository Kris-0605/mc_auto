import requests
import paramiko
import json
from time import sleep
from traceback import format_exc
from multiprocessing import Pool, Value
from tqdm import tqdm

droplet = None
ssh_global = None
mc_stdin, mc_stdout, mc_stderr = None, None, None

def load_config():
    global DIGITAL_OCEAN_AUTH_TOKEN, WORLD_FILE_DIRECTORY, SSH_KEY_ID, FABRIC_INSTALLER_URL, PLAYER_WHITELIST
    with open("config.json", "r") as f:
        config = json.load(f)
    DIGITAL_OCEAN_AUTH_TOKEN = {"Authorization": "Bearer " + config["DIGITAL_OCEAN_AUTH_TOKEN"]}
    WORLD_FILE_DIRECTORY = config["WORLD_FILE_DIRECTORY"]
    SSH_KEY_ID = config["SSH_KEY_ID"] # Put the key in ssh_key.pub
    FABRIC_INSTALLER_URL = config["FABRIC_INSTALLER_URL"]
    PLAYER_WHITELIST = config["PLAYER_WHITELIST"]

def execute_and_wait(ssh, command):
    stdin, stdout, stderr = ssh.exec_command(command)
    return stdout.channel.recv_exit_status()

def connect(droplet):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(droplet["droplet"]["networks"]["v4"][0]["ip_address"], username="root", key_filename="ssh_key.pub")
    return ssh

def install(droplet, connect, execute_and_wait, FABRIC_INSTALLER_URL, PLAYER_WHITELIST):
    import paramiko
    print("[Process 1] Connecting via SSH...")
    ssh = connect(droplet)
    print("[Process 1] Updating droplet...")
    execute_and_wait(ssh, "export DEBIAN_FRONTEND=noninteractive && apt update && apt upgrade -y")
    print("[Process 1] Installing Java and 7-zip...")
    execute_and_wait(ssh, "export DEBIAN_FRONTEND=noninteractive && apt install openjdk-17-jre p7zip-full -y")
    print("[Process 1] Downloading Fabric installer...")
    execute_and_wait(ssh, f"wget {FABRIC_INSTALLER_URL}") # Update to desired version
    print("[Process 1] Installing Fabric...")
    execute_and_wait(ssh, f"java -jar {FABRIC_INSTALLER_URL.split('/')[-1]} server -downloadMinecraft")
    print("[Process 1] Altering EULA...")
    execute_and_wait(ssh, "rm eula.txt")
    execute_and_wait(ssh, "echo eula=true > eula.txt")
    print("[Process 1] Setting whitelist...")
    execute_and_wait(ssh, "rm whitelist.json")
    execute_and_wait(ssh, f"echo {json.dumps(PLAYER_WHITELIST)} > whitelist.json")
    print("[Process 1] Terminating...")
    ssh.close()

def upload(droplet, connect, execute_and_wait, WORLD_FILE_DIRECTORY):
    import paramiko, subprocess, os
    from tqdm import tqdm
    print("[Process 2] Connecting via SSH...")
    ssh = connect(droplet)
    print("[Process 2] Compressing world and mods...")
    subprocess.Popen(f'7z a -mx9 in.7z "{WORLD_FILE_DIRECTORY}" mods', shell=True, stdout=subprocess.DEVNULL).wait()
    local_hash = "not the same"
    remote_hash = "different"
    while local_hash != remote_hash:
        print("[Process 2] Uploading archive...")
        size = os.path.getsize("in.7z")
        sftp = ssh.open_sftp()
        # Sending an archive seems to be more consistent than sending individual files
        with tqdm(total=size, unit="B", unit_scale=True) as pbar:
            pbar.total = size
            sftp.put("in.7z", "in.7z", confirm=True, callback=lambda bytes_transferred, total_bytes: pbar.update(bytes_transferred - pbar.n))
        print("[Process 2] Verifying archive integrity...")
        local_hash = subprocess.Popen("certutil -hashfile in.7z MD5", shell=True, stdout=subprocess.PIPE).stdout.read().decode("utf-8").split("\r\n")[1]
        remote_hash = ssh.exec_command("md5sum in.7z")[1].read().decode("utf-8").split(" ")[0]
        if local_hash != remote_hash:
            print("[Process 2] Archive integrity check failed, retrying...")
    print("[Process 2] Extracting archive...")
    # We need to wait for 7-zip to be installed, it will return a non-zero error code when not installed
    while execute_and_wait(ssh, "7z x in.7z"):
        sleep(1)
    print("[Process 2] Renaming world folder...")
    world_name = WORLD_FILE_DIRECTORY.split("\\")[-1]
    execute_and_wait(ssh, f'mv "{world_name}" world')
    print("[Process 2] Removing archives...")
    execute_and_wait(ssh, "rm in.7z")
    os.remove("in.7z")
    print("[Process 2] Setting server properties...")
    with sftp.open("server.properties", "w") as f:
        # Adjust to your liking
        f.write('#Minecraft server properties\n#Sat Jul 15 01:40:56 UTC 2023\nenable-jmx-monitoring=false\nrcon.port=25575\nlevel-seed=\ngamemode=survival\nenable-command-block=false\nenable-query=false\ngenerator-settings={}\nenforce-secure-profile=true\nlevel-name=world\nmotd=A Minecraft Server\nquery.port=25565\npvp=true\ngenerate-structures=true\nmax-chained-neighbor-updates=1000000\ndifficulty=hard\nnetwork-compression-threshold=256\nmax-tick-time=60000\nrequire-resource-pack=false\nuse-native-transport=true\nmax-players=20\nonline-mode=true\nenable-status=true\nallow-flight=false\ninitial-disabled-packs=\nbroadcast-rcon-to-ops=true\nview-distance=16\nserver-ip=\nresource-pack-prompt=\nallow-nether=true\nserver-port=25565\nenable-rcon=false\nsync-chunk-writes=true\nop-permission-level=4\nprevent-proxy-connections=false\nhide-online-players=false\nresource-pack=\nentity-broadcast-range-percentage=500\nsimulation-distance=10\nrcon.password=\nplayer-idle-timeout=0\nforce-gamemode=false\nrate-limit=0\nhardcore=true\nwhite-list=true\nbroadcast-console-to-ops=true\nspawn-npcs=true\nspawn-animals=true\nfunction-permission-level=2\ninitial-enabled-packs=vanilla,fabric\nlevel-type=minecraft\\:normal\ntext-filtering-config=\nspawn-monsters=true\nenforce-whitelist=true\nspawn-protection=16\nresource-pack-sha1=\nmax-world-size=29999984')
    sftp.close()
    print("[Process 2] Terminating...")
    ssh.close()

def recover():
    global droplet
    id = input(" Droplet ID >>> ")
    print("Rebuilding droplet...")
    requests.post(f"https://api.digitalocean.com/v2/droplets/{id}/actions", headers=DIGITAL_OCEAN_AUTH_TOKEN, json={"type": "rebuild", "image": "ubuntu-20-04-x64"})
    sleep(10)
    print("Getting droplet...")
    droplet = get_droplet(id)
    main()

def create():
    global droplet
    print("Creating droplet...")
    droplet = requests.post("https://api.digitalocean.com/v2/droplets", headers=DIGITAL_OCEAN_AUTH_TOKEN, json={
        "name": "mc-auto",
        "region": "lon1", # Change if you're not British
        "size": "s-4vcpu-8gb-amd", # If you take out my proxy server and my Spaces, then I have $68 free, which is 800 hours of playtime on a $56 a month droplet, if I remember to destroy it when not being used
        "image": "ubuntu-20-04-x64",
        "ssh_keys": [SSH_KEY_ID], # Get from /v2/account/keys endpoint
    }).json()
    print("Created droplet with ID", droplet["droplet"]["id"])
    main()

def get_droplet(id):
    global droplet
    droplet = requests.get(f"https://api.digitalocean.com/v2/droplets/{id}", headers=DIGITAL_OCEAN_AUTH_TOKEN).json()
    return droplet

def main():
    global droplet, ssh_global, mc_stdin, mc_stdout, mc_stderr
    print("Connecting via SSH...")
    ssh_global = paramiko.SSHClient()
    ssh_global.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    retries = 0
    while True:
        try:
            # Sometimes the IPv4 address isn't returned on droplet creation, so we keep trying to get it until it's there
            get_droplet(droplet["droplet"]["id"])
            ssh_global.connect(droplet["droplet"]["networks"]["v4"][0]["ip_address"], username="root", key_filename="ssh_key.pub")
            print("Connected!")
            break
        except:
            retries += 1
            print(f"Failed to connect, retrying in {retries} {'seconds' if retries != 1 else 'second'}...")
            sleep(retries)
    
    # Runs the installation of the server and uploading of the world files in independent parallel processes
    pool = Pool(processes=2)
    pool.apply_async(install, args=(droplet, connect, execute_and_wait, FABRIC_INSTALLER_URL, PLAYER_WHITELIST))
    pool.apply_async(upload, args=(droplet, connect, execute_and_wait, WORLD_FILE_DIRECTORY))
    pool.close()
    pool.join()
    print("Starting server...")
    mc_stdin, mc_stdout, mc_stderr = ssh_global.exec_command("java -Xmx6G -jar fabric-server-launch.jar nogui")
    print(f"The server IP is {droplet['droplet']['networks']['v4'][0]['ip_address']}. Please wait for it to start.")

def download():
    print("Stopping server...")
    mc_stdin.write("stop\n")
    mc_stdin.flush()
    print("Waiting for server to stop...")
    mc_stdout.channel.recv_exit_status()
    print("Compressing backup...")
    execute_and_wait(ssh_global, "7z a -mx9 out.7z world")
    print("Downloading backup...")
    sftp = ssh_global.open_sftp()
    sftp.get("out.7z", "out.7z")
    sftp.close()

def destroy():
    print("Closing SSH session...")
    ssh_global.close()
    print("Destroying droplet...")
    http_status_code = requests.delete(f"https://api.digitalocean.com/v2/droplets/{droplet['droplet']['id']}", headers=DIGITAL_OCEAN_AUTH_TOKEN).status_code
    print("Success") if http_status_code == 204 else print(f"Failed with HTTP status code {http_status_code}")


if __name__ == "__main__":
    print('''
Welcome to Kris's MC Auto!
My PC sucks and I have free DigitalOcean educational credit, so this script spawns a Minecraft server on a DigitalOcean droplet.
Note that this script requires you to "pip install requests paramiko tqdm".
You must also have "7z" in your PATH.
You need a file "ssh_key.pub" that contains an SSH key linked to your DigitalOcean account.
Put the mods you want to upload in the mods folder in this directory.
You should also set the JSON parameters appropriately, see the load config function above.
Commands:
          
          create - Creates the droplet, uploads the world specified, starts the Minecraft server and returns the public IP address.
          recover - Asks for a droplet ID, rebuilds the droplet and then does the same as create.
          download - Stops the server and creates a 7-zip backup of the world directory, downloading it to "out.7z". If you don't do this it will be lost!
          destroy - Destroys the droplet. This will delete the world if you didn't download it! If you do not delete, you will be charged for usage until you do!
          exit - Exits the program.

''')
    load_config()
    while True:
        try:
            match input(" >>> "):
                case "create":
                    create()
                case "recover":
                    recover()
                case "download":
                    download()
                case "destroy":
                    destroy()
                case "exit":
                    if ssh_global:
                        ssh_global.close()
                    break
                case _:
                    print("Invalid command.")
        except Exception:
            print(format_exc())