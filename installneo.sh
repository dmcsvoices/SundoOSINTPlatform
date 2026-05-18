sudo bash -c '
# 1. Install Java (required for Neo4j)
apt update -qq
apt install -y openjdk-21-jre-headless wget apt-transport-https ca-certificates curl gnupg

# 2. Add Neo4j repository
wget -O - https://debian.neo4j.com/neotechnology.gpg.key | gpg --dearmor -o /usr/share/keyrings/neo4j.gpg
echo "deb [signed-by=/usr/share/keyrings/neo4j.gpg] https://debian.neo4j.com stable 5" > /etc/apt/sources.list.d/neo4j.list
apt update -qq
apt install -y neo4j

# 3. Configure for non-standard port and all interfaces
cat >> /etc/neo4j/neo4j.conf << "EOF"

# Sundo Pi custom settings
server.default_listen_address=0.0.0.0
server.bolt.listen_address=0.0.0.0:17687
server.bolt.advertised_address=0.0.0.0:17687

# Memory tuning for Pi 5 (8GB RAM)
server.memory.heap.initial_size=512m
server.memory.heap.max_size=512m
server.memory.pagecache.size=1g
EOF

# 4. Enable and start
systemctl enable neo4j
systemctl start neo4j

# 5. Set initial password
neo4j-admin dbms set-initial-password sundo

echo "Neo4j installed and configured on port 17687"
'

