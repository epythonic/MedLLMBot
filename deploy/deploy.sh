#!/usr/bin/env bash
# Обновление демо-сервера до свежего main: код → пересборка контейнеров → проверка, что всё поднялось.
#
# Запускается из GitHub Actions по SSH после слияния PR в main. Ключ CI привязан в authorized_keys
# к этому скрипту (command="…/deploy/deploy.sh"), поэтому ничего другого этим ключом выполнить нельзя.
# Можно запустить и вручную: ./deploy/deploy.sh
#
# Если на stdin пришёл текст (CI передаёт .env, собранный из секретов GitHub Environment «demo»),
# он становится новым .env с правами 600. При ручном запуске из терминала .env не трогается.
#
# Тело — в функции: git обновляет и этот файл, а bash читает скрипт по ходу выполнения.
# Функция целиком разбирается до запуска, поэтому новая версия файла не смешается со старой.

set -euo pipefail

main() {
    cd "$(dirname "$0")/.."

    if [[ ! -t 0 ]]; then
        local env_content
        env_content="$(cat)"
        if [[ -n "$env_content" ]]; then
            echo "→ Обновляю .env из секретов GitHub"
            (umask 077 && printf '%s\n' "$env_content" > .env.new)
            mv .env.new .env
        fi
    fi

    echo "→ Получаю свежий main"
    git fetch --quiet origin main
    # Только перемотка вперёд: правки, сделанные прямо на сервере, остановят деплой, а не потеряются
    git merge --ff-only --quiet origin/main
    echo "  версия $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

    echo "→ Пересобираю и перезапускаю контейнеры"
    docker compose --profile https up -d --build --remove-orphans

    echo "→ Жду, пока админка станет здоровой"
    local health=""
    for _ in $(seq 1 45); do
        health="$(docker compose ps admin --format '{{.Health}}')"
        [[ "$health" == "healthy" ]] && break
        sleep 2
    done
    if [[ "$health" != "healthy" ]]; then
        echo "✗ Админка не поднялась (статус: ${health:-нет}). Последние строки лога:" >&2
        docker compose logs --tail 50 admin >&2
        exit 1
    fi

    if [[ "$(docker compose ps bot --format '{{.State}}')" != "running" ]]; then
        echo "✗ Бот не запущен. Последние строки лога:" >&2
        docker compose logs --tail 50 bot >&2
        exit 1
    fi

    # Старые образы после пересборки занимают место на маленьком сервере
    docker image prune -f >/dev/null
    echo "✓ Демо обновлено до $(git rev-parse --short HEAD)"
}

main "$@"
exit
