#!/usr/bin/env python3
"""
Тест подключения к прокси
Проверяет, что прокси Clash Verge доступен из Ubuntu VirtualBox
"""

import httpx
import sys

def test_proxy(proxy_url: str = "http://10.0.2.2:7897"):
    """Проверка прокси подключения."""
    print(f"\n{'='*60}")
    print(f"Тестирование прокси: {proxy_url}")
    print(f"{'='*60}\n")
    
    client = httpx.Client(
        proxy=proxy_url,
        timeout=httpx.Timeout(30.0, connect=10.0)
    )
    
    try:
        # Тест 1: Проверка IP
        print("Тест 1: Проверка внешнего IP...")
        response = client.get("https://httpbin.org/ip")
        
        if response.status_code == 200:
            ip_data = response.json()
            print(f"✓ Прокси работает!")
            print(f"  Ваш внешний IP: {ip_data.get('origin')}")
        else:
            print(f"✗ Ошибка: статус {response.status_code}")
            return False
        
        # Тест 2: Проверка заголовков
        print("\nТест 2: Проверка заголовков...")
        response = client.get("https://httpbin.org/headers")
        
        if response.status_code == 200:
            print(f"✓ Заголовки получены успешно")
        else:
            print(f"✗ Ошибка: статус {response.status_code}")
            return False
        
        # Тест 3: Проверка Google
        print("\nТест 3: Проверка доступа к Google...")
        response = client.get("https://www.google.com")
        
        if response.status_code == 200:
            print(f"✓ Google доступен через прокси")
            print(f"  Размер ответа: {len(response.text)} байт")
        else:
            print(f"✗ Ошибка: статус {response.status_code}")
            return False
        
        print("\n" + "="*60)
        print("✓✓✓ ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО! ✓✓✓")
        print("="*60 + "\n")
        return True
        
    except httpx.ConnectError as e:
        print(f"\n✗✗✗ ОШИБКА ПОДКЛЮЧЕНИЯ ✗✗✗")
        print(f"Не удалось подключиться к прокси: {e}")
        print("\nПроверьте:")
        print("  1. Clash Verge запущен на Windows")
        print("  2. Порт прокси правильный (обычно 7897)")
        print("  3. 'Allow LAN' включен в настройках Clash")
        print("  4. Брандмауэр Windows не блокирует порт")
        return False
        
    except httpx.ProxyError as e:
        print(f"\n✗✗✗ ОШИБКА ПРОКСИ ✗✗✗")
        print(f"Прокси вернул ошибку: {e}")
        print("\nПроверьте настройки прокси в Clash Verge")
        return False
        
    except httpx.TimeoutException as e:
        print(f"\n✗✗✗ ТАЙМАУТ ✗✗✗")
        print(f"Запрос превысил время ожидания: {e}")
        print("\nВозможные причины:")
        print("  1. Медленное интернет-соединение")
        print("  2. Прокси перегружен")
        print("  3. Неправильные настройки прокси")
        return False
        
    except Exception as e:
        print(f"\n✗✗✗ НЕОЖИДАННАЯ ОШИБКА ✗✗✗")
        print(f"Тип: {type(e).__name__}")
        print(f"Сообщение: {e}")
        return False
        
    finally:
        client.close()


def main():
    """Main entry point."""
    import os
    
    # Попытка загрузить из .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
        proxy_url = os.getenv("PROXY_URL", "http://10.0.2.2:7897")
    except ImportError:
        proxy_url = "http://10.0.2.2:7897"
    
    print("\n" + "="*60)
    print("  ТЕСТ ПОДКЛЮЧЕНИЯ К ПРОКСИ CLASH VERGE")
    print("="*60)
    
    # Можно указать свой прокси через аргумент командной строки
    if len(sys.argv) > 1:
        proxy_url = sys.argv[1]
    
    success = test_proxy(proxy_url)
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
