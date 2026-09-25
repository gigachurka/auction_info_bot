"""Test script to check if Chrome and ChromeDriver are working"""
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    logger.info("Testing Chrome with Selenium...")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    
    logger.info("Initializing Chrome driver...")
    driver = webdriver.Chrome(options=options)
    logger.info("Chrome driver created successfully!")
    
    logger.info("Navigating to test page...")
    driver.get("https://www.google.com")
    logger.info(f"Page title: {driver.title}")
    
    logger.info("Quitting driver...")
    driver.quit()
    logger.info("Test completed successfully!")
    
except Exception as e:
    logger.error(f"Test failed: {e}")
    import traceback
    traceback.print_exc()
