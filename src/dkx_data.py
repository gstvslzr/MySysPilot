# Generates the DKX data block (Docker examples, templates, tutorial links) for ui.html (v1.4).
import json

BASICS = [
    ("A", "Check that Docker works", "Confirm the client and the daemon both answer, then run the official test image.", [
        ("docker version", "Client and Server versions. If 'Server' is missing, the daemon is stopped or you lack permission."),
        ("docker info", "Storage driver, number of containers/images, rootless mode, cgroup version."),
        ("docker run --rm hello-world", "Pulls a tiny image, runs it once, prints a message, then removes the container (--rm)."),
        ("sudo usermod -aG docker $USER   # then log out and back in", "Lets your user run docker without sudo. Membership in 'docker' is root-equivalent on this machine."),
    ], [("Run hello-world", {"image": "hello-world"})]),
    ("B", "Images: download, list, remove", "An image is a read-only template. Containers are started from images.", [
        ("docker pull nginx:alpine", "Download an image. 'nginx' is the repository, 'alpine' the tag. No tag means ':latest'."),
        ("docker images", "List local images with size and age (same as 'docker image ls')."),
        ("docker image inspect nginx:alpine", "Full JSON metadata: layers, env, exposed ports, entrypoint."),
        ("docker image rm nginx:alpine", "Remove an image. Fails while a container still uses it."),
        ("docker image prune", "Remove dangling (untagged) images. Add -a to remove every image not used by a container."),
    ], []),
    ("C", "Containers: run, list, stop, remove", "A container is a running (or stopped) instance of an image.", [
        ("docker run -d --name web -p 127.0.0.1:8080:80 nginx:alpine", "-d detached, --name a fixed name, -p host:container port. Binding to 127.0.0.1 keeps it off your LAN."),
        ("docker ps", "Running containers. 'docker ps -a' includes stopped ones."),
        ("docker stop web && docker start web", "Stop (SIGTERM, then SIGKILL after 10 s) and start again. State on the container's filesystem is kept."),
        ("docker restart web", "Stop + start in one step."),
        ("docker rm -f web", "Remove the container (-f stops it first). Data not in a volume is lost."),
        ("docker run --rm -it alpine sh", "Interactive throw-away shell: -i keep STDIN open, -t allocate a TTY, --rm delete on exit."),
    ], [("Run nginx on 127.0.0.1:8080", {"image": "nginx:alpine", "name": "web-demo", "ports": ["127.0.0.1:8080:80"]})]),
    ("D", "Look inside: logs, exec, stats", "Day-to-day troubleshooting commands.", [
        ("docker logs -f --tail 100 web", "Follow the last 100 lines of STDOUT/STDERR."),
        ("docker exec -it web sh", "Open a shell inside a running container (use bash if the image has it)."),
        ("docker stats", "Live CPU / memory / network / block I/O per container. Ctrl+C to quit."),
        ("docker top web", "Processes running inside the container."),
        ("docker inspect web", "Full JSON: IP address, mounts, restart policy, health."),
        ("docker cp web:/etc/nginx/nginx.conf ./nginx.conf", "Copy files between the container and the host (both directions)."),
    ], []),
    ("E", "Keep data: volumes and bind mounts", "Container filesystems are disposable. Put data you want to keep in a volume or a host folder.", [
        ("docker volume create appdata", "Named volume managed by Docker (stored under /var/lib/docker/volumes)."),
        ("docker run -d --name db -v appdata:/var/lib/postgresql/data -e POSTGRES_PASSWORD=change-me postgres:16", "Mount the named volume at the database's data path. Replace the password."),
        ("docker run --rm -v \"$PWD\":/work -w /work alpine ls -la", "Bind mount: share the current host folder into the container."),
        ("docker volume ls && docker volume inspect appdata", "List volumes and show where one lives on disk."),
        ("docker volume prune", "Remove volumes not used by any container (asks first). Data is gone permanently."),
    ], []),
    ("F", "Networking", "Containers on the same user-defined network reach each other by name.", [
        ("docker network create appnet", "User-defined bridge network with built-in DNS."),
        ("docker run -d --name api --network appnet nginx:alpine", "Attach at start time."),
        ("docker run --rm --network appnet alpine wget -qO- http://api", "Another container reaches 'api' by name."),
        ("docker network connect appnet web", "Attach an already running container."),
        ("docker network ls && docker network inspect appnet", "List networks / show members and subnet."),
    ], []),
    ("G", "Build your own image", "A Dockerfile is the recipe. See the Dockerfile templates tab for ready-made ones.", [
        ("docker build -t myapp:1.0 .", "Build from the Dockerfile in the current folder and tag it myapp:1.0."),
        ("docker run -d --name myapp -p 127.0.0.1:8000:8000 myapp:1.0", "Run what you built."),
        ("docker history myapp:1.0", "Layers and the size each instruction added."),
        ("docker tag myapp:1.0 registry.example.com/team/myapp:1.0", "Add a registry name before pushing."),
        ("docker login registry.example.com && docker push registry.example.com/team/myapp:1.0", "Push to a registry. Credentials are stored in ~/.docker/config.json (use a credential helper)."),
    ], []),
    ("H", "Multi-container apps with Compose", "Describe services, networks and volumes in compose.yaml. See the Compose templates tab.", [
        ("docker compose up -d", "Create and start everything in compose.yaml in the current folder."),
        ("docker compose ps", "Status of this project's services."),
        ("docker compose logs -f", "Follow logs of all services (add a service name to filter)."),
        ("docker compose pull && docker compose up -d", "Update: pull newer images, recreate changed containers."),
        ("docker compose down", "Stop and remove containers and the default network. Add -v to also delete volumes."),
    ], []),
    ("I", "Clean up disk space", "Docker keeps stopped containers, old images and build cache until you remove them.", [
        ("docker system df", "How much space images, containers, volumes and build cache use."),
        ("docker container prune", "Remove all stopped containers."),
        ("docker builder prune", "Remove build cache."),
        ("docker system prune", "Stopped containers + unused networks + dangling images + build cache. Add -a for all unused images, --volumes for volumes."),
    ], []),
    ("J", "Restart policies and updates", "Keep services running across reboots.", [
        ("docker run -d --restart unless-stopped --name web nginx:alpine", "Restart after crashes and reboots unless you stopped it yourself."),
        ("docker update --restart unless-stopped web", "Change the policy of an existing container."),
        ("docker pull nginx:alpine && docker rm -f web && docker run -d --name web ... nginx:alpine", "Manual update: pull, remove, re-create with the same options (Compose does this for you)."),
    ], []),
]

DOCKERFILES = [
    {"id": "static", "name": "Static website (nginx)", "tag": "my-static-site", "run": {"ports": ["127.0.0.1:8080:80"]},
     "desc": "Serve a folder of HTML/CSS/JS with nginx on Alpine. Smallest useful image.",
     "files": {
        "Dockerfile": "FROM nginx:alpine\n# Copy the site into nginx's default web root\nCOPY site/ /usr/share/nginx/html/\nEXPOSE 80\n",
        "site/index.html": (
            "<!doctype html>\n"
            "<html><head><meta charset=\"utf-8\"><title>It works</title></head>\n"
            "<body style=\"font-family:sans-serif;padding:40px\"><h1>Hello from Docker</h1><p>Edit site/index.html and rebuild.</p></body></html>\n"
        ),
        ".dockerignore": ".git\n*.md\n",
     }},
    {"id": "python", "name": "Python web app (Flask + gunicorn)", "tag": "my-python-app", "run": {"ports": ["127.0.0.1:8000:8000"]},
     "desc": "Slim Python image, dependencies cached in their own layer, runs as a non-root user.",
     "files": {
        "Dockerfile": (
            "FROM python:3.12-slim\n"
            "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
            "WORKDIR /app\n"
            "# Install dependencies first so this layer is cached between code changes\n"
            "COPY requirements.txt .\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n"
            "COPY . .\n"
            "# Do not run as root inside the container\n"
            "RUN useradd --create-home --uid 10001 app\n"
            "USER app\n"
            "EXPOSE 8000\n"
            "CMD [\"gunicorn\", \"--bind\", \"0.0.0.0:8000\", \"app:app\"]\n"
        ),
        "requirements.txt": "flask==3.*\ngunicorn==23.*\n",
        "app.py": "from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.get(\"/\")\ndef index():\n    return {\"status\": \"ok\", \"message\": \"Hello from a Python container\"}\n",
        ".dockerignore": "__pycache__/\n*.pyc\n.venv/\n.git\n",
     }},
    {"id": "node", "name": "Node.js app (Express)", "tag": "my-node-app", "run": {"ports": ["127.0.0.1:3000:3000"]},
     "desc": "Production install with npm, runs as the image's built-in 'node' user.",
     "files": {
        "Dockerfile": (
            "FROM node:22-alpine\n"
            "ENV NODE_ENV=production\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm install --omit=dev\n"
            "COPY . .\n"
            "USER node\n"
            "EXPOSE 3000\n"
            "CMD [\"node\", \"server.js\"]\n"
        ),
        "package.json": "{\n  \"name\": \"my-node-app\",\n  \"version\": \"1.0.0\",\n  \"private\": true,\n  \"main\": \"server.js\",\n  \"dependencies\": { \"express\": \"^4.21.0\" }\n}\n",
        "server.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "app.get('/', (req, res) => res.json({ status: 'ok', message: 'Hello from a Node container' }));\n"
            "app.listen(3000, () => console.log('listening on 3000'));\n"
        ),
        ".dockerignore": "node_modules\nnpm-debug.log\n.git\n",
     }},
    {"id": "go", "name": "Go service (multi-stage build)", "tag": "my-go-app", "run": {"ports": ["127.0.0.1:8090:8080"]},
     "desc": "Compile in a full Go image, ship only the static binary on a scratch base (a few MB).",
     "files": {
        "Dockerfile": (
            "# ---- build stage\n"
            "FROM golang:1.23-alpine AS build\n"
            "WORKDIR /src\n"
            "COPY go.mod ./\n"
            "COPY *.go ./\n"
            "RUN CGO_ENABLED=0 go build -trimpath -ldflags=\"-s -w\" -o /out/app .\n"
            "\n"
            "# ---- runtime stage: nothing but the binary\n"
            "FROM scratch\n"
            "COPY --from=build /out/app /app\n"
            "USER 65534:65534\n"
            "EXPOSE 8080\n"
            "ENTRYPOINT [\"/app\"]\n"
        ),
        "go.mod": "module example.com/app\n\ngo 1.23\n",
        "main.go": (
            "package main\n"
            "\n"
            "import (\n"
            "\t\"fmt\"\n"
            "\t\"net/http\"\n"
            ")\n"
            "\n"
            "func main() {\n"
            "\thttp.HandleFunc(\"/\", func(w http.ResponseWriter, r *http.Request) {\n"
            "\t\tfmt.Fprintln(w, \"Hello from a Go container\")\n"
            "\t})\n"
            "\thttp.ListenAndServe(\":8080\", nil)\n"
            "}\n"
        ),
     }},
    {"id": "script", "name": "Scheduled script / one-shot job (Bash on Alpine)", "tag": "my-job", "run": None,
     "desc": "Package a shell script with its tools. Run it with 'docker run --rm my-job' from cron or a systemd timer.",
     "files": {
        "Dockerfile": (
            "FROM alpine:3.20\n"
            "RUN apk add --no-cache bash curl jq tzdata\n"
            "COPY job.sh /usr/local/bin/job.sh\n"
            "RUN chmod +x /usr/local/bin/job.sh\n"
            "USER nobody\n"
            "ENTRYPOINT [\"/usr/local/bin/job.sh\"]\n"
        ),
        "job.sh": "#!/usr/bin/env bash\nset -euo pipefail\necho \"[$(date -Is)] job started\"\n# your work here\necho \"[$(date -Is)] job finished\"\n",
     }},
]

COMPOSE = [
    {"id": "wordpress", "name": "WordPress + MariaDB", "url": "http://127.0.0.1:8080",
     "desc": "Two services, two named volumes. The database password is generated for you and stored in .env (mode 600).",
     "files": {
        "compose.yaml": (
            "services:\n"
            "  db:\n"
            "    image: mariadb:11\n"
            "    restart: unless-stopped\n"
            "    environment:\n"
            "      MARIADB_DATABASE: wordpress\n"
            "      MARIADB_USER: wordpress\n"
            "      MARIADB_PASSWORD: ${DB_PASSWORD}\n"
            "      MARIADB_RANDOM_ROOT_PASSWORD: \"1\"\n"
            "    volumes:\n"
            "      - db_data:/var/lib/mysql\n"
            "\n"
            "  wordpress:\n"
            "    image: wordpress:latest\n"
            "    depends_on:\n"
            "      - db\n"
            "    restart: unless-stopped\n"
            "    ports:\n"
            "      - \"127.0.0.1:8080:80\"   # local only; remove 127.0.0.1: to expose on the LAN\n"
            "    environment:\n"
            "      WORDPRESS_DB_HOST: db\n"
            "      WORDPRESS_DB_USER: wordpress\n"
            "      WORDPRESS_DB_PASSWORD: ${DB_PASSWORD}\n"
            "      WORDPRESS_DB_NAME: wordpress\n"
            "    volumes:\n"
            "      - wp_data:/var/www/html\n"
            "\n"
            "volumes:\n"
            "  db_data:\n"
            "  wp_data:\n"
        ),
        ".env": "DB_PASSWORD={{RANDOM}}\n",
     }},
    {"id": "postgres", "name": "PostgreSQL + Adminer", "url": "http://127.0.0.1:8081",
     "desc": "Database with a web admin UI. Log in to Adminer with system 'PostgreSQL', server 'db', user 'app' and the password from .env.",
     "files": {
        "compose.yaml": (
            "services:\n"
            "  db:\n"
            "    image: postgres:16\n"
            "    restart: unless-stopped\n"
            "    environment:\n"
            "      POSTGRES_USER: app\n"
            "      POSTGRES_DB: app\n"
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n"
            "    ports:\n"
            "      - \"127.0.0.1:5432:5432\"\n"
            "    volumes:\n"
            "      - pg_data:/var/lib/postgresql/data\n"
            "    healthcheck:\n"
            "      test: [\"CMD-SHELL\", \"pg_isready -U app -d app\"]\n"
            "      interval: 10s\n"
            "      retries: 5\n"
            "\n"
            "  adminer:\n"
            "    image: adminer:latest\n"
            "    restart: unless-stopped\n"
            "    depends_on:\n"
            "      - db\n"
            "    ports:\n"
            "      - \"127.0.0.1:8081:8080\"\n"
            "\n"
            "volumes:\n"
            "  pg_data:\n"
        ),
        ".env": "POSTGRES_PASSWORD={{RANDOM}}\n",
     }},
    {"id": "uptime", "name": "Uptime Kuma (service monitor)", "url": "http://127.0.0.1:3001",
     "desc": "Self-hosted uptime/status monitoring. Image tag per the project README (louislam/uptime-kuma:2).",
     "files": {
        "compose.yaml": (
            "services:\n"
            "  uptime-kuma:\n"
            "    image: louislam/uptime-kuma:2\n"
            "    restart: unless-stopped\n"
            "    ports:\n"
            "      - \"127.0.0.1:3001:3001\"\n"
            "    volumes:\n"
            "      - kuma_data:/app/data\n"
            "\n"
            "volumes:\n"
            "  kuma_data:\n"
        ),
     }},
    {"id": "portainer", "name": "Portainer CE (web UI for Docker)", "url": "https://127.0.0.1:9443",
     "desc": (
         "Mounts /var/run/docker.sock — this gives the container full root-equivalent control of Docker. Portainer collects anonymous usage statistics by default; you can turn that off in its Settings."
     ),
     "files": {
        "compose.yaml": (
            "services:\n"
            "  portainer:\n"
            "    image: portainer/portainer-ce:lts\n"
            "    restart: unless-stopped\n"
            "    ports:\n"
            "      - \"127.0.0.1:9443:9443\"   # HTTPS UI (self-signed certificate)\n"
            "    volumes:\n"
            "      - /var/run/docker.sock:/var/run/docker.sock\n"
            "      - portainer_data:/data\n"
            "\n"
            "volumes:\n"
            "  portainer_data:\n"
        ),
     }},
    {"id": "openwebui", "name": "Open WebUI (chat UI for a local Ollama)", "url": "http://127.0.0.1:3000",
     "desc": (
         "Connects to Ollama running on this computer through host.docker.internal. Ollama must listen on an address the container can reach (by default it listens on 127.0.0.1 only) — see the Open WebUI docs. Telemetry opt-outs are set explicitly."
     ),
     "files": {
        "compose.yaml": (
            "services:\n"
            "  open-webui:\n"
            "    image: ghcr.io/open-webui/open-webui:main\n"
            "    restart: unless-stopped\n"
            "    ports:\n"
            "      - \"127.0.0.1:3000:8080\"\n"
            "    extra_hosts:\n"
            "      - \"host.docker.internal:host-gateway\"\n"
            "    environment:\n"
            "      OLLAMA_BASE_URL: http://host.docker.internal:11434\n"
            "      WEBUI_SECRET_KEY: ${WEBUI_SECRET_KEY}\n"
            "      SCARF_NO_ANALYTICS: \"true\"\n"
            "      DO_NOT_TRACK: \"true\"\n"
            "      ANONYMIZED_TELEMETRY: \"false\"\n"
            "    volumes:\n"
            "      - open_webui:/app/backend/data\n"
            "\n"
            "volumes:\n"
            "  open_webui:\n"
        ),
        ".env": "WEBUI_SECRET_KEY={{RANDOM}}\n",
     }},
]

# Every URL below was opened and checked on 2026-10-17.
LINKS = [
    ("Start here (official Docker docs)", [
        ("Get started", "https://docs.docker.com/get-started/", "Docker's own learning path: concepts, then hands-on guides.", "Official · free"),
        ("What is Docker?", "https://docs.docker.com/get-started/docker-overview/", "Architecture in plain terms: client, daemon, images, containers, registries.", "Official · free"),
        ("What is a container?", "https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-a-container/", "Short concept page with a hands-on part.", "Official · free"),
        ("What is an image?", "https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-an-image/", "Layers, tags and where images come from.", "Official · free"),
        ("Containerize an application", "https://docs.docker.com/get-started/tutorials/run-an-app/", "Step-by-step: from source code to a running container.", "Official · free"),
        ("Docker CLI cheat sheet (PDF)", "https://docs.docker.com/get-started/docker_cheatsheet.pdf", "One page of the most used commands.", "Official · free"),
    ]),
    ("How-to topics", [
        ("Writing a Dockerfile", "https://docs.docker.com/get-started/docker-concepts/building-images/writing-a-dockerfile/", "FROM, COPY, RUN, CMD explained with an example.", "Official · free"),
        ("Multi-stage builds", "https://docs.docker.com/get-started/docker-concepts/building-images/multi-stage-builds/", "Smaller, safer images by separating build and runtime.", "Official · free"),
        ("Publishing and exposing ports", "https://docs.docker.com/get-started/docker-concepts/running-containers/publishing-ports/", "What -p really does.", "Official · free"),
        ("Persisting container data", "https://docs.docker.com/get-started/docker-concepts/running-containers/persisting-container-data/", "Volumes vs. container filesystem.", "Official · free"),
        ("Docker Compose Quickstart", "https://docs.docker.com/compose/gettingstarted/", "Build a two-service app (Python + Redis) with compose.yaml.", "Official · free"),
        ("Building best practices", "https://docs.docker.com/build/building/best-practices/", "Base images, caching, non-root users, .dockerignore.", "Official · free"),
    ]),
    ("Linux setup & reference", [
        ("Install Docker Engine on Ubuntu", "https://docs.docker.com/engine/install/ubuntu/", "Official apt repository method (what the Setup tab automates).", "Official · free"),
        ("Linux post-installation steps", "https://docs.docker.com/engine/install/linux-postinstall/", "Run docker without sudo, start on boot, and the security implications.", "Official · free"),
        ("Dockerfile reference", "https://docs.docker.com/reference/dockerfile/", "Every instruction and option.", "Official · free"),
        ("Compose file reference", "https://docs.docker.com/reference/compose-file/", "Every key you can use in compose.yaml.", "Official · free"),
    ]),
    ("Hands-on labs & community tutorials", [
        ("Play with Docker Classroom", "https://training.play-with-docker.com/", "Labs that run in the browser — nothing to install. The browser playground asks for a Docker Hub account.", "Docker-sponsored · free"),
        ("PWD — IT Pros & SysAdmins, Stage 1", "https://training.play-with-docker.com/ops-stage1", "The basics from an operations point of view.", "Docker-sponsored · free"),
        ("PWD — Developers, Stage 1", "https://training.play-with-docker.com/dev-stage1", "The basics for developers, up to multi-service apps.", "Docker-sponsored · free"),
        ("Docker Curriculum", "https://docker-curriculum.com/", "Long single-page beginner tutorial by Prakhar Srivastav. Some sections may be dated.", "Community · free"),
        ("Educational resources (books & courses)", "https://docs.docker.com/get-started/resources/", "Docker's list of books and video courses, including Spanish titles. Many are paid.", "Official list · mixed"),
    ]),
]

def js():
    return "const DKX=" + json.dumps({"basics": BASICS, "dockerfiles": DOCKERFILES, "compose": COMPOSE, "links": LINKS},
                                    ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ";"

if __name__ == "__main__":
    print(js())
