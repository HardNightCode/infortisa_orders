pipeline {
  agent any

  environment {
    // --- Ajustes comunes ---
    MODULE_NAME   = 'infortisa_orders'
    ODOO_BIN      = '/usr/bin/odoo'
    ODOO_CONF     = '/etc/odoo/odoo.conf'
    ADDON_DIR     = "/opt/odoo/custom/addons/${env.MODULE_NAME}"
    SERVICE_NAME  = 'odoo'
    DB_NAME       = 'odoo_nexus'
    // errores (palabras a buscar en logs para fallos)
    ERR_PATTERNS  = "ERROR|Traceback|CRITICAL|psycopg2|odoo.exceptions|xmlrpc.client.Fault|IntegrityError"

    // --- STAGE ---
    STAGE_HOST    = '10.0.100.160'
    STAGE_USER    = 'deploy'              // <--- cambia si usas otro usuario
    STAGE_CREDS   = 'ssh-stage'           // <--- ID de credencial Jenkins válida para STAGE

    // --- PROD ---
    PROD_HOST     = '10.0.100.152'
    PROD_USER     = 'deploy'              // <--- cambia si usas otro usuario
    PROD_CREDS    = 'ssh-prod'            // <--- ID de credencial Jenkins válida para PROD
  }

  options {
    ansiColor('xterm')
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '25'))
    disableConcurrentBuilds()
  }

  triggers {
    // Si usas GitHub webhook, deja este; si no, usa pollSCM
    githubPush()
    // pollSCM('* * * * *')  // opcional
  }

  stages {

    stage('Checkout') {
      steps {
        // Jenkins ya obtiene Jenkinsfile del repo; aquí solo informe
        echo "SCM: ${env.GIT_URL ?: 'git@github.com:HardNightCode/infortisa_orders.git'}"
      }
    }

    stage('Deploy & Verify STAGE') {
      steps {
        sshagent(credentials: [env.STAGE_CREDS]) {
          script {
            deployAndVerify(env.STAGE_USER, env.STAGE_HOST)
          }
        }
      }
    }

    stage('Promote to PROD (auto)') {
      when { expression { currentBuild.currentResult == 'SUCCESS' } }
      steps {
        sshagent(credentials: [env.PROD_CREDS]) {
          script {
            deployAndVerify(env.PROD_USER, env.PROD_HOST)
          }
        }
      }
    }
  }

  post {
    success {
      echo '✅ Pipeline OK: STAGE y PROD desplegados sin errores.'
    }
    failure {
      echo '❌ Pipeline FAILED. Revisa la etapa donde ocurrió y los logs impresos arriba.'
    }
  }
}

// ============ FUNCIONES GROOVY REUTILIZABLES ============

def deployAndVerify(String user, String host) {
  // Helpers: ejecutar remoto con bash y quoting robusto
  def runRemote = { String cmd ->
    sh(
      label: "remote ${host}",
      script: """bash -lc ${quote("""
set -euo pipefail
ssh -o StrictHostKeyChecking=no ${user}@${host} bash -lc ${quote(cmd)}
""")}
      """
    )
  }

  def runRemoteOut = { String cmd ->
    sh(
      returnStdout: true,
      script: """bash -lc ${quote("""
ssh -o StrictHostKeyChecking=no ${user}@${host} bash -lc ${quote(cmd)}
""")}
    """
    ).trim()
  }

  // 1) Obtener commit previo para posible rollback (¡sin romper las comillas!)
  def prevCommit = runRemoteOut("""
if [ -d ${env.ADDON_DIR}/.git ]; then
  cd ${env.ADDON_DIR}
  git rev-parse HEAD~1 2>/dev/null || true
fi
""")

  echo "Prev commit en ${host}: ${prevCommit ?: '(no disponible, primer deploy)'}"

  try {
    // 2) GIT: traer cambios del repo remoto (crea carpeta si no existe)
    runRemote("""
if [ ! -d ${env.ADDON_DIR} ]; then
  mkdir -p "$(dirname ${env.ADDON_DIR})"
  cd "$(dirname ${env.ADDON_DIR})"
  git clone git@github.com:HardNightCode/${env.MODULE_NAME}.git
fi
cd ${env.ADDON_DIR}
git fetch --all --prune
git reset --hard origin/main
git rev-parse HEAD
""")

    // 3) Upgrade de módulo en Odoo
    runRemote("""
sudo -n -u odoo ${env.ODOO_BIN} -c ${env.ODOO_CONF} -d ${env.DB_NAME} -u ${env.MODULE_NAME} --stop-after-init
""")

    // 4) Restart servicio
    runRemote("""
sudo -n systemctl restart ${env.SERVICE_NAME}
sudo -n systemctl is-active --quiet ${env.SERVICE_NAME}
""")

    // 5) Health check
    runRemote("""
curl -fsS http://localhost:8069/web/login >/dev/null
echo 'Health OK'
""")

    // 6) Revisar logs recientes y fallar si hay patrones de error
    sh(
      script: """bash -lc ${quote("""
set -euo pipefail
echo "==== Últimas 500 líneas de journalctl en ${host} ===="
ssh -o StrictHostKeyChecking=no ${user}@${host} 'sudo -n journalctl -u ${env.SERVICE_NAME} -n 500 --no-pager || true' | tee /tmp/journal_${host}.log
echo "==== Grep de errores en ${host} (si coincide, fallará) ===="
if egrep -i '${env.ERR_PATTERNS}' /tmp/journal_${host}.log; then
  echo 'Se detectaron errores en logs.'
  exit 1
fi
""")}
    )

    echo "✅ ${host}: Deploy y verificación OK"

  } catch (err) {
    echo "❌ ${host}: FALLO detectado. Iniciando ROLLBACK…"
    if (prevCommit) {
      // 7) Rollback al commit anterior
      runRemote("""
cd ${env.ADDON_DIR}
git reset --hard ${prevCommit}
""")
      // Reintento de upgrade + restart + health
      try {
        runRemote("""
sudo -n -u odoo ${env.ODOO_BIN} -c ${env.ODOO_CONF} -d ${env.DB_NAME} -u ${env.MODULE_NAME} --stop-after-init
sudo -n systemctl restart ${env.SERVICE_NAME}
sudo -n systemctl is-active --quiet ${env.SERVICE_NAME}
curl -fsS http://localhost:8069/web/login >/dev/null
""")
      } catch (err2) {
        echo "⚠️ ${host}: Rollback aplicado pero verificación falló. Revisa logs."
      }
    } else {
      echo "⚠️ ${host}: No hay commit previo para rollback."
    }

    // 8) Imprimir logs tras el error para diagnosticar
    sh(
      script: """bash -lc ${quote("""
set -euo pipefail
echo "==== LOGS tras el error en ${host} (últimas 800 líneas) ===="
ssh -o StrictHostKeyChecking=no ${user}@${host} 'sudo -n journalctl -u ${env.SERVICE_NAME} -n 800 --no-pager || true'
""")}
    )
    error("Abortando pipeline por fallo en ${host}.")
  }
}

@NonCPS
def quote(String s) {
  // Escapa comillas simples para inyectar el bloque dentro de bash -lc '...'
  return "'${s.replace("'", "'\"'\"'")}'"
}
