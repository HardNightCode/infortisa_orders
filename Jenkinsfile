pipeline {       
  agent any  
      
  environment {
    MODULE_NAME   = 'infortisa_orders'
    ODOO_BIN      = '/usr/bin/odoo'
    ODOO_CONF     = '/etc/odoo/odoo.conf'
    ADDON_DIR     = "/opt/odoo/custom/addons/${env.MODULE_NAME}"
    SERVICE_NAME  = 'odoo'
    DB_NAME       = 'odoo_nexus'
    ERR_PATTERNS  = "ERROR|CRITICAL|Traceback|odoo.exceptions|psycopg2|OperationalError|xmlrpc.client.Fault|IntegrityError"

    STAGE_HOST    = '10.0.100.160'
    STAGE_USER    = 'deploy'
    STAGE_CREDS   = 'ssh-stage'

    PROD_HOST     = '10.0.100.152'
    PROD_USER     = 'deploy'
    PROD_CREDS    = 'ssh-prod'
  }

  options {
    ansiColor('xterm')
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '25'))
    disableConcurrentBuilds()
  }

  triggers {
    githubPush()
  }

  stages {
    stage('Checkout') {
      steps {
        echo "SCM: ${env.GIT_URL ?: 'git@github.com:HardNightCode/infortisa_orders.git'}"
      }
    }

    stage('Deploy & Verify STAGE') {
      steps {
        sshagent(credentials: [env.STAGE_CREDS]) {
          script { deployAndVerify(env.STAGE_USER, env.STAGE_HOST) }
        }
      }
    }

    stage('Promote to PROD (auto)') {
      when { expression { currentBuild.currentResult == 'SUCCESS' } }
      steps {
        sshagent(credentials: [env.PROD_CREDS]) {
          script { deployAndVerify(env.PROD_USER, env.PROD_HOST) }
        }
      }
    }
  }

  post {
    success { echo '✅ Pipeline OK: STAGE y PROD desplegados sin errores.' }
    failure { echo '❌ Pipeline FAILED. Revisa la etapa donde ocurrió y los logs impresos arriba.' }
  }
}

// ================== FUNCIONES ================== 

def deployAndVerify(String user, String host) {

  // Helpers: fuerzan bash via heredoc (sin shebangs con opciones)
  def sshRun = { String cmd ->
    sh(
      label: "remote ${host}",
      script: """bash -euo pipefail <<'LOCAL_EOF'
ssh -o StrictHostKeyChecking=no ${user}@${host} 'bash -s' <<'REMOTE_EOF'
set -euo pipefail
${cmd}
REMOTE_EOF
LOCAL_EOF
"""
    )
  }

  def sshRunOut = { String cmd ->
    return sh(
      returnStdout: true,
      script: """bash -euo pipefail <<'LOCAL_EOF'
ssh -o StrictHostKeyChecking=no ${user}@${host} 'bash -s' <<'REMOTE_EOF'
set -euo pipefail
${cmd}
REMOTE_EOF
LOCAL_EOF
"""
    ).trim()
  }

  final String addonDir    = env.ADDON_DIR
  final String addonParent = addonDir.contains('/') ? addonDir.substring(0, addonDir.lastIndexOf('/')) : '.'

  // 1) Commit previo EXACTO (para rollback)
  def prevCommit = sshRunOut("""
if [ -d "${addonDir}/.git" ]; then
  sudo -u odoo git -C "${addonDir}" rev-parse HEAD 2>/dev/null || true
fi
""")
  echo "Prev commit en ${host}: ${prevCommit ?: '(no disponible, primer deploy)'}"

  try {
    // 2) Git update como odoo (y safe.directory)
    sshRun("""
sudo install -d -o odoo -g odoo -m 775 "${addonParent}"

if [ ! -d "${addonDir}/.git" ]; then
  sudo -u odoo git clone git@github.com:HardNightCode/${env.MODULE_NAME}.git "${addonDir}"
fi

sudo chown -R odoo:odoo "${addonDir}"
sudo -u odoo git config --global --add safe.directory "${addonDir}" || true

sudo -u odoo git -C "${addonDir}" fetch --all --prune
sudo -u odoo git -C "${addonDir}" reset --hard origin/main
sudo -u odoo git -C "${addonDir}" rev-parse HEAD
""")

    // 3) Upgrade de módulo (one-shot)
    sshRun("""sudo -n -u odoo ${env.ODOO_BIN} -c ${env.ODOO_CONF} -d ${env.DB_NAME} -u ${env.MODULE_NAME} --stop-after-init""")

    // 4) Reinicio servicio
    sshRun("""
sudo -n systemctl restart ${env.SERVICE_NAME}
sudo -n systemctl is-active --quiet ${env.SERVICE_NAME}
""")

    // 5) Health check con reintentos (hasta 120s)
    sshRun("""
set +e
for i in \$(seq 1 60); do
  if curl -fsS http://127.0.0.1:8069/web/login >/dev/null; then
    echo "Health-check OK"
    exit 0
  fi
  sleep 2
done
echo "Health-check FAILED: Odoo no responde en 120s" >&2
exit 1
""")

    // 6) Logs + detección de errores
    sh(
      script: """bash -euo pipefail <<'LOCAL_EOF'
echo "==== Últimas 500 líneas de journalctl en ${host} ===="
ssh -o StrictHostKeyChecking=no ${user}@${host} 'sudo -n journalctl -u ${env.SERVICE_NAME} -n 500 --no-pager || true' | tee /tmp/journal_${host}.log
echo "==== Grep de errores en ${host} (si coincide, fallará) ===="
if egrep -i '${env.ERR_PATTERNS}' /tmp/journal_${host}.log; then
  echo 'Se detectaron errores en logs.'
  exit 1
fi
LOCAL_EOF
"""
    )

    echo "✅ ${host}: Deploy y verificación OK"

  } catch (err) {
    echo "❌ ${host}: FALLO detectado. Iniciando ROLLBACK…"
    if (prevCommit) {
      sshRun("""sudo -u odoo git -C "${addonDir}" reset --hard ${prevCommit}""")
      try {
        sshRun("""sudo -n -u odoo ${env.ODOO_BIN} -c ${env.ODOO_CONF} -d ${env.DB_NAME} -u ${env.MODULE_NAME} --stop-after-init""")
        sshRun("""sudo -n systemctl restart ${env.SERVICE_NAME}""")
        sshRun("""sudo -n systemctl is-active --quiet ${env.SERVICE_NAME}""")
        sshRun("""
set +e
for i in \$(seq 1 60); do
  if curl -fsS http://127.0.0.1:8069/web/login >/dev/null; then
    exit 0
  fi
  sleep 2
done
exit 1
""")
      } catch (err2) {
        echo "⚠️ ${host}: Rollback aplicado pero verificación falló. Revisa logs."
      }
    } else {
      echo "⚠️ ${host}: No hay commit previo para rollback."
    }

    // Logs tras el error
    sh(
      script: """bash -euo pipefail <<'LOCAL_EOF'
echo "==== LOGS tras el error en ${host} (últimas 250 líneas) ===="
ssh -o StrictHostKeyChecking=no ${user}@${host} 'sudo -n journalctl -u ${env.SERVICE_NAME} -n 250 --no-pager || true'
LOCAL_EOF
"""
    )
    error("Abortando pipeline por fallo en ${host}.")
  }
}
